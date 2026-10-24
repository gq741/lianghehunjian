[CmdletBinding()]
param(
    [string]$DownloadDir = $PSScriptRoot,
    [string]$DestinationParent = $PSScriptRoot,
    [switch]$RunCheck
)

# Windows PowerShell 5.1 + .NET only. No Python or administrator rights needed to restore.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
Add-Type -AssemblyName System.IO.Compression
$token = [Guid]::NewGuid().ToString('N')
$report = [ordered]@{
    schema = 1; operation = 'restore-release'; started_utc = [DateTime]::UtcNow.ToString('o')
    restore_status = 'NOT_STARTED'; environment_status = 'NOT_RUN'; paid_calls = 0
    stage = 'input'; destination = $null; temporary_zip = $null; staging = $null
    checked_parts = @(); checked_files = 0; error = $null; exit_code = 1
}
$reportPath = $null
$archive = $null
$merged = $null
$staging = $null
$exitCode = 1

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Assert-Sha256($Value, [string]$Label) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^[a-fA-F0-9]{64}$') { throw "$Label 的 SHA256 格式错误。" }
}

function Get-Size($Value, [string]$Label) {
    if ([string]$Value -notmatch '^\d+$') { throw "$Label 的字节数格式错误。" }
    try { return [long]$Value } catch { throw "$Label 的字节数超出支持范围。" }
}

function Get-NormalizedDirectory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    # Keep E:\ as E:\, never drive-relative E: when the chosen parent is a volume root.
    if ($full.Length -le $root.Length) { return $root }
    return $full.TrimEnd('\')
}

function Get-SafeRelativePath([string]$Path, [bool]$AllowDirectory = $false) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Contains('\') -or $Path.StartsWith('/') -or $Path.Contains(':')) {
        throw "归档路径无效：$Path"
    }
    $value = $Path
    if ($AllowDirectory -and $value.EndsWith('/')) { $value = $value.Substring(0, $value.Length - 1) }
    foreach ($piece in $value.Split('/')) {
        if ($piece -eq '' -or $piece -eq '.' -or $piece -eq '..' -or $piece -match '[<>:"|?*\x00-\x1f]' -or
            $piece.EndsWith('.') -or $piece.EndsWith(' ') -or
            $piece -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            throw "归档路径无效：$Path"
        }
    }
    return $value
}

function Assert-Space([string]$Parent, [long]$Required) {
    $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($Parent))
    if ($drive.IsReady -and $drive.AvailableFreeSpace -lt $Required) {
        $need = [Math]::Round($Required / 1GB, 2)
        throw "目标磁盘空间不足；此阶段至少还需要 $need GiB。请换一个空间充足的目标目录。"
    }
}

try {
    $download = (Get-Item -LiteralPath $DownloadDir).FullName
    $parentItem = Get-Item -LiteralPath $DestinationParent
    if (-not $parentItem.PSIsContainer -or -not (Get-Item -LiteralPath $download).PSIsContainer) {
        throw '下载目录和目标父目录必须是已存在的文件夹。'
    }
    $parent = Get-NormalizedDirectory $parentItem.FullName
    $reportPath = Join-Path $parent ("LiangHeMix-restore-report-$token.json")
    $target = Join-Path $parent 'LiangHeMix'
    $report.destination = $target
    if (Test-Path -LiteralPath $target) { throw "目标已存在，原文件保持不变：$target。请换空的目标父目录。" }

    $partsFile = Join-Path $download 'PARTS.json'
    $parts = Get-Content -LiteralPath $partsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($parts.schema -ne 1 -or $parts.root_dir -cne 'LiangHeMix' -or
        $parts.archive_name -isnot [string] -or $parts.archive_name -cnotmatch '^LiangHeMix-[A-Za-z0-9._-]+\.zip$') {
        throw 'PARTS.json 版本、归档名称或根目录不符合约定。'
    }
    Assert-Sha256 $parts.archive_sha256 '总归档'
    Assert-Sha256 $parts.manifest_sha256 '文件清单'
    $archiveBytes = Get-Size $parts.archive_bytes '总归档'
    $rows = @($parts.parts)
    if ($rows.Count -lt 1 -or $rows.Count -gt 99 -or $archiveBytes -le 0) { throw '分块清单为空或数量无效。' }
    Assert-Space $parent ($archiveBytes + 64MB)
    $report.stage = 'verify_parts'
    [long]$partTotal = 0
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $row = $rows[$i]
        $expectedName = $parts.archive_name + ('.part{0:D2}' -f ($i + 1))
        if ($row.name -cne $expectedName) { throw "分块名称或顺序错误：应为 $expectedName" }
        Assert-Sha256 $row.sha256 $expectedName
        $size = Get-Size $row.bytes $expectedName
        $path = Join-Path $download $expectedName
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少分块：$expectedName。请下载全部分块后重试。" }
        $item = Get-Item -LiteralPath $path
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -ne $size) {
            throw "分块大小不符或分块是链接：$expectedName"
        }
        Write-Host ("校验分块 {0}/{1}：{2}" -f ($i + 1), $rows.Count, $expectedName)
        if ((Get-Sha256 $path) -ine $row.sha256) { throw "分块 SHA256 不符：$expectedName。请重新下载该分块。" }
        $partTotal += $size
        $report.checked_parts += $expectedName
    }
    if ($partTotal -ne $archiveBytes) { throw '分块总大小与归档大小不一致。' }

    $report.stage = 'merge'
    $merged = Join-Path $parent (".LiangHeMix-$token.zip")
    $report.temporary_zip = $merged
    $output = [IO.File]::Open($merged, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        foreach ($row in $rows) {
            $inputStream = [IO.File]::OpenRead((Join-Path $download $row.name))
            try { $inputStream.CopyTo($output, 4MB) } finally { $inputStream.Dispose() }
        }
    } finally { $output.Dispose() }
    if ((Get-Item -LiteralPath $merged).Length -ne $archiveBytes -or (Get-Sha256 $merged) -ine $parts.archive_sha256) {
        throw '合并后的 ZIP 校验失败；分块可能在读取期间发生改变。'
    }

    $report.stage = 'inspect_zip'
    $zipStream = [IO.File]::OpenRead($merged)
    $archive = New-Object IO.Compression.ZipArchive ($zipStream, [IO.Compression.ZipArchiveMode]::Read, $false)
    $entryPaths = New-Object 'Collections.Generic.Dictionary[string,bool]' ([StringComparer]::OrdinalIgnoreCase)
    $entries = @()
    [long]$unpackedBytes = 0
    foreach ($entry in $archive.Entries) {
        $isDirectory = $entry.FullName.EndsWith('/')
        $relative = Get-SafeRelativePath $entry.FullName $true
        if ($relative -cne 'LiangHeMix' -and -not $relative.StartsWith('LiangHeMix/', [StringComparison]::Ordinal)) {
            throw "ZIP 条目不在指定根目录内：$relative"
        }
        if ($relative -ceq 'LiangHeMix' -and -not $isDirectory) { throw 'ZIP 根目录被文件占用。' }
        if ($entryPaths.ContainsKey($relative)) { throw "ZIP 含重复或大小写冲突路径：$relative" }
        $external = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$entry.ExternalAttributes), 0)
        $kind = ($external -shr 16) -band 0xf000
        if (($external -band 0x400) -ne 0 -or ($kind -ne 0 -and $kind -ne 0x8000 -and $kind -ne 0x4000)) {
            throw "ZIP 含链接、重解析点或非常规文件：$relative"
        }
        if ($isDirectory -and $entry.Length -ne 0) { throw "ZIP 目录带有文件数据：$relative" }
        $entryPaths.Add($relative, $isDirectory)
        if (-not $isDirectory) { $unpackedBytes += $entry.Length }
        $entries += [PSCustomObject]@{ Entry = $entry; Relative = $relative; Directory = $isDirectory }
    }
    foreach ($name in $entryPaths.Keys) {
        $slash = $name.LastIndexOf('/')
        while ($slash -ge 0) {
            $prefix = $name.Substring(0, $slash)
            if ($entryPaths.ContainsKey($prefix) -and -not $entryPaths[$prefix]) { throw "ZIP 文件与目录冲突：$prefix" }
            $slash = $prefix.LastIndexOf('/')
        }
    }
    if (-not $entryPaths.ContainsKey('LiangHeMix/FILES.sha256.json') -or $entryPaths['LiangHeMix/FILES.sha256.json']) {
        throw 'ZIP 缺少 FILES.sha256.json。'
    }
    Assert-Space $parent ($unpackedBytes + 64MB)

    $report.stage = 'extract'
    $staging = Join-Path $parent (".LiangHeMix-stage-$token")
    $report.staging = $staging
    if (Test-Path -LiteralPath $staging) { throw '临时目录已存在，请重新运行以生成新目录。' }
    [IO.Directory]::CreateDirectory($staging) | Out-Null
    $boundary = [IO.Path]::GetFullPath($staging).TrimEnd('\') + '\'
    foreach ($record in $entries) {
        $destination = [IO.Path]::GetFullPath((Join-Path $staging $record.Relative.Replace('/', '\')))
        if (-not $destination.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) { throw '解压路径越界。' }
        if ($record.Directory) { [IO.Directory]::CreateDirectory($destination) | Out-Null; continue }
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
        $inputStream = $record.Entry.Open()
        $output = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $inputStream.CopyTo($output, 4MB) } finally { $output.Dispose(); $inputStream.Dispose() }
    }
    $archive.Dispose(); $archive = $null
    $restoredRoot = Join-Path $staging 'LiangHeMix'
    $report.stage = 'verify_files'
    $manifestPath = Join-Path $restoredRoot 'FILES.sha256.json'
    if ((Get-Sha256 $manifestPath) -ine $parts.manifest_sha256) { throw '内部文件清单 SHA256 不符。' }
    # PS 5.1 ConvertFrom-Json emits an array as one pipeline object. Enumerate the variable instead.
    $decodedManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $fileRows = @($decodedManifest)
    if ($fileRows.Count -lt 1) { throw '内部文件清单为空。' }
    $expected = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($row in $fileRows) {
        $relative = Get-SafeRelativePath $row.path
        if ($relative -ieq 'FILES.sha256.json' -or -not $expected.Add($relative)) { throw "内部清单包含重复或保留路径：$relative" }
        Assert-Sha256 $row.sha256 $relative
        $size = Get-Size $row.bytes $relative
        $path = Join-Path $restoredRoot $relative.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne $size -or
            (Get-Sha256 $path) -ine $row.sha256) { throw "文件校验失败：$relative" }
        $report.checked_files++
    }
    foreach ($path in [IO.Directory]::EnumerateFiles($restoredRoot, '*', [IO.SearchOption]::AllDirectories)) {
        $relative = $path.Substring($restoredRoot.Length + 1).Replace('\', '/')
        if ($relative -ine 'FILES.sha256.json' -and -not $expected.Contains($relative)) { throw "存在清单外文件：$relative" }
    }

    $report.stage = 'publish'
    if (Test-Path -LiteralPath $target) { throw '目标目录在还原期间被创建；停止且保留原有目录。' }
    [IO.Directory]::Move($restoredRoot, $target)
    $report.restore_status = 'PASS'
    Write-Host "文件还原与完整性校验通过：$target"
    # Only remove the exact temporary file made by this run; no recursive deletion.
    if ([IO.Path]::GetFullPath($merged) -ceq (Join-Path $parent (".LiangHeMix-$token.zip"))) {
        Remove-Item -LiteralPath $merged
        $report.temporary_zip = $null
    }
    if ([IO.Directory]::GetFileSystemEntries($staging).Length -eq 0) { [IO.Directory]::Delete($staging, $false); $report.staging = $null }
    $exitCode = 0
    if ($RunCheck) {
        $report.stage = 'environment_check'
        $python = Join-Path $target 'runtime/python.exe'
        $checker = Join-Path $target 'app/tools/portable_check.py'
        $checkOutput = Join-Path $target 'logs/selfcheck.json'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $checker -PathType Leaf)) {
            throw '还原完成，但缺少免费自检入口。'
        }
        & $python -I -B -X utf8 $checker --package-root $target --output $checkOutput
        $checkExit = $LASTEXITCODE
        $report.environment_exit_code = $checkExit
        $report.environment_report = $checkOutput
        if ($checkExit -ne 0) {
            $report.environment_status = 'NEEDS_ATTENTION'
            $exitCode = 2
            Write-Host "文件还原已通过；环境检测尚未通过。请让 WorkBuddy 查看 $checkOutput，按说明安装随包系统组件后重新自检。"
        } else {
            $report.environment_status = 'PASS'
            Write-Host '免费自检通过。请打开 LiangHeMix 文件夹中的量禾混剪.exe，并填写自己的 API Key。'
        }
    } else {
        Write-Host '环境尚未检测。请进入还原后的文件夹，运行“免费自检.cmd”，通过后再打开软件。'
    }
    $report.stage = 'complete'
} catch {
    $report.error = $_.Exception.Message
    if ($report.restore_status -eq 'PASS') { $report.environment_status = 'NEEDS_ATTENTION'; $exitCode = 2 }
    else { $report.restore_status = 'FAIL'; $exitCode = 1 }
    Write-Host ("操作停止：" + $report.error) -ForegroundColor Red
    Write-Host '既有目录和下载分块保持不变。若有临时文件，路径会保存在诊断报告中；请勿将未通过的目录当作完整软件使用。'
} finally {
    if ($null -ne $archive) { $archive.Dispose() }
    $report.exit_code = $exitCode
    $report.finished_utc = [DateTime]::UtcNow.ToString('o')
    if ($null -ne $reportPath) {
        try {
            $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding UTF8
            Write-Host "诊断报告：$reportPath"
        } catch { Write-Host ('诊断报告写入失败：' + $_.Exception.Message) -ForegroundColor Red; $exitCode = 1 }
    }
}
exit $exitCode
