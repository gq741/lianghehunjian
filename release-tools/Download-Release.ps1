[CmdletBinding()]
param(
    [string]$DownloadDir = $PSScriptRoot,
    [switch]$DownloadOnly
)

# Public, anonymous downloads only. Windows PowerShell 5.1 / .NET; no Python required.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue' # This process only; per-file Write-Host progress remains visible.
Set-StrictMode -Version 2
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-DownloadSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Get-ReleaseMetadata {
    # Exactly one release metadata request, not one API request per part.
    $uri = 'https://api.github.com/repos/gq741/lianghehunjian/releases/tags/v2026.10.29'
    return Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 60 -Headers @{
        'User-Agent' = 'LiangHeMix-Public-Downloader/2026.10.29'
        'Accept' = 'application/vnd.github+json'
    }
}

function Receive-AssetBytes([string]$Uri, [string]$Path) {
    # No WebSession, cookies, credentials or Authorization header are supplied.
    Invoke-WebRequest -Uri $Uri -Method Get -UseBasicParsing -MaximumRedirection 10 -TimeoutSec 1800 `
        -UserAgent 'LiangHeMix-Public-Downloader/2026.10.29' -OutFile $Path | Out-Null
}

function Assert-ReleaseAsset($Asset) {
    if ($Asset.name -isnot [string] -or $Asset.size -le 0 -or $Asset.state -cne 'uploaded' -or
        $Asset.digest -isnot [string] -or $Asset.digest -cnotmatch '^sha256:[0-9a-fA-F]{64}$') {
        throw 'GitHub 附件缺少大小、已上传状态或 SHA256 digest；停止下载。'
    }
    [long]$size = $Asset.size
    if ($size -ne $Asset.size) { throw 'GitHub 附件大小无效。' }
    $expected = 'https://github.com/gq741/lianghehunjian/releases/download/v2026.10.29/' + [Uri]::EscapeDataString($Asset.name)
    $uri = [Uri]$Asset.browser_download_url
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne 'https' -or $uri.DnsSafeHost -cne 'github.com' -or
        $uri.Port -ne 443 -or $uri.UserInfo -ne '' -or $uri.Fragment -ne '' -or $uri.Query -ne '' -or
        $Asset.browser_download_url -cne $expected) {
        throw ('附件下载地址不属于指定仓库和版本：' + $Asset.name)
    }
}

function Get-DownloadAsset($Asset, [string]$Directory) {
    $target = Join-Path $Directory $Asset.name
    $expectedHash = $Asset.digest.Substring(7).ToLowerInvariant()
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $item.Length -ne [long]$Asset.size -or (Get-DownloadSha256 $target) -cne $expectedHash) {
            throw ('同名文件已存在但校验不符，未覆盖：' + $target + '。请先移走该文件，再重试。')
        }
        Write-Host ('已校验并复用：' + $Asset.name)
        return [PSCustomObject]@{ name = $Asset.name; result = 'REUSED'; attempts = 0; bytes = $item.Length; sha256 = $expectedHash }
    }
    $lastReason = 'UNKNOWN'
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        # Every temporary file is created by this invocation, never an existing user file.
        $temporary = Join-Path $Directory ('.LiangHeMix-download-' + [Guid]::NewGuid().ToString('N') + '-' + $Asset.name + '.partial')
        $reason = 'NETWORK_OR_IO'
        try {
            $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            $stream.Dispose()
            Write-Host ('下载 ' + $Asset.name + '（第 ' + $attempt + '/3 次）')
            Receive-AssetBytes $Asset.browser_download_url $temporary
            if ((Get-Item -LiteralPath $temporary).Length -ne [long]$Asset.size) {
                $reason = 'SIZE_MISMATCH'; throw '下载字节数不符。'
            }
            if ((Get-DownloadSha256 $temporary) -cne $expectedHash) {
                $reason = 'SHA256_MISMATCH'; throw '下载 SHA256 不符。'
            }
            # File.Move refuses to overwrite if another process creates the destination meanwhile.
            [IO.File]::Move($temporary, $target)
            return [PSCustomObject]@{ name = $Asset.name; result = 'DOWNLOADED'; attempts = $attempt; bytes = [long]$Asset.size; sha256 = $expectedHash }
        } catch {
            $lastReason = $reason + '/' + $_.Exception.GetType().Name
            Write-Host ('本次未通过：' + $Asset.name + '；' + $lastReason)
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                # Exact non-recursive cleanup, only for the unique file created immediately above.
                Remove-Item -LiteralPath $temporary
            }
            if ($attempt -lt 3) { Start-Sleep -Seconds (2 * $attempt) }
        }
    }
    throw ('下载失败或缺块：' + $Asset.name + '；已尝试 3 次，最后原因 ' + $lastReason + '。再次运行会复用已经校验的其他附件。')
}

function Invoke-DownloadRelease([string]$Directory, [bool]$Only) {
    $report = [ordered]@{
        schema = 1; product = '量禾混剪'; release = 'v2026.10.29'; paid_calls = 0
        started_utc = [DateTime]::UtcNow.ToString('o'); status = 'STARTED'; stage = 'input'
        download_dir = $null; assets = @(); error = $null; exit_code = 1
    }
    $reportPath = $null
    $exitCode = 1
    try {
        $full = [IO.Path]::GetFullPath($Directory)
        $root = [IO.Path]::GetPathRoot($full)
        if ($full.Length -gt $root.Length) { $full = $full.TrimEnd('\') }
        if (-not (Test-Path -LiteralPath $full)) { [IO.Directory]::CreateDirectory($full) | Out-Null }
        if (-not (Get-Item -LiteralPath $full).PSIsContainer) { throw '下载目录必须是文件夹。' }
        $report.download_dir = $full
        $reportPath = Join-Path $full 'download-report.json'
        Write-Host '建议放在 D:\LiangHeDownload 等短目录；请预留分块、合并 ZIP 和完整软件所需空间。'
        $report.stage = 'release_metadata'
        $release = Get-ReleaseMetadata
        if ($release.tag_name -cne 'v2026.10.29' -or $release.draft -ne $false) { throw '公开发布版本不符合约定。' }
        $requiredNames = @('PARTS.json', 'Restore-Release.ps1', 'Restore-Release.cmd', 'WORKBUDDY.md',
                           'START_HERE.txt', 'LICENSE.txt', 'ReleaseNotes.md', 'SHA256SUMS.txt')
        $assets = New-Object 'Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($asset in $release.assets) {
            $selected = $requiredNames -ccontains $asset.name -or $asset.name -cmatch '^LiangHeMix-20261029\.zip\.part(?:0[1-9]|[1-9][0-9])$'
            if (-not $selected) { continue }
            if ($assets.ContainsKey($asset.name)) { throw ('发布附件重名：' + $asset.name) }
            Assert-ReleaseAsset $asset
            $assets.Add($asset.name, $asset)
        }
        foreach ($name in $requiredNames) {
            if (-not $assets.ContainsKey($name)) { throw ('发布尚不完整，缺少附件：' + $name) }
        }
        $report.stage = 'download_manifest'
        $report.assets += Get-DownloadAsset $assets['PARTS.json'] $full
        $parts = Get-Content -LiteralPath (Join-Path $full 'PARTS.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($parts.schema -ne 1 -or $parts.root_dir -cne 'LiangHeMix' -or $parts.archive_name -cne 'LiangHeMix-20261029.zip' -or
            $parts.archive_sha256 -cnotmatch '^[a-fA-F0-9]{64}$' -or $parts.manifest_sha256 -cnotmatch '^[a-fA-F0-9]{64}$') {
            throw 'PARTS.json 格式不符合发布约定。'
        }
        $partRows = @($parts.parts)
        if ($partRows.Count -lt 1 -or $partRows.Count -gt 99 -or $assets.Count -ne ($requiredNames.Count + $partRows.Count)) {
            throw '发布分块数量与 PARTS.json 不一致；请等待发布完成后重试。'
        }
        [long]$partBytes = 0
        for ($i = 0; $i -lt $partRows.Count; $i++) {
            $row = $partRows[$i]
            $name = 'LiangHeMix-20261029.zip.part{0:D2}' -f ($i + 1)
            if ($row.name -cne $name -or -not $assets.ContainsKey($name)) { throw ('缺少或顺序错误的分块：' + $name) }
            if ($row.bytes -le 0 -or [long]$row.bytes -ne $assets[$name].size -or
                $row.sha256 -cnotmatch '^[a-fA-F0-9]{64}$' -or ('sha256:' + $row.sha256.ToLowerInvariant()) -cne $assets[$name].digest.ToLowerInvariant()) {
                throw ('GitHub 元信息与 PARTS.json 不一致：' + $name)
            }
            $partBytes += [long]$row.bytes
        }
        if ($partBytes -ne [long]$parts.archive_bytes) { throw '分块总字节数与归档大小不一致。' }
        $report.stage = 'download_assets'
        foreach ($name in $requiredNames) {
            if ($name -ceq 'PARTS.json') { continue }
            $report.assets += Get-DownloadAsset $assets[$name] $full
        }
        foreach ($row in $partRows) { $report.assets += Get-DownloadAsset $assets[$row.name] $full }
        $report.status = 'DOWNLOAD_PASS'
        Write-Host '全部发布附件已下载并通过大小、SHA256 校验；未下载 GitHub 自动生成的 Source code。'
        if ($Only) {
            $report.status = 'DOWNLOAD_ONLY_PASS'
            $report.stage = 'complete'
            $exitCode = 0
        } else {
            $report.stage = 'restore_and_free_check'
            $powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
            & $powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $full 'Restore-Release.ps1') `
                -DownloadDir $full -DestinationParent $full -RunCheck | Out-Host
            $restoreExit = $LASTEXITCODE
            $report.restore_exit_code = $restoreExit
            if ($restoreExit -eq 0) { $exitCode = 0; $report.status = 'RESTORE_AND_ENVIRONMENT_PASS' }
            elseif ($restoreExit -eq 2) { $exitCode = 2; $report.status = 'ENVIRONMENT_NEEDS_ATTENTION' }
            else { $exitCode = 1; $report.status = 'RESTORE_FAILED' }
            $report.stage = 'complete'
        }
    } catch {
        $report.status = 'FAIL'
        $report.error = $_.Exception.Message
        Write-Host ('操作停止：' + $report.error) -ForegroundColor Red
        $exitCode = 1
    } finally {
        $report.exit_code = $exitCode
        $report.finished_utc = [DateTime]::UtcNow.ToString('o')
        if ($null -ne $reportPath) {
            try { $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding UTF8; Write-Host ('下载报告：' + $reportPath) }
            catch { Write-Host '下载报告写入失败。' -ForegroundColor Red; $exitCode = 1 }
        }
    }
    return $exitCode
}

$result = Invoke-DownloadRelease -Directory $DownloadDir -Only ([bool]$DownloadOnly)
exit [int]$result
