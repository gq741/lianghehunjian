param([string]$Target = "", [switch]$Rollback, [switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
try {
    Add-Type -AssemblyName System.Windows.Forms
    function Is-Package([string]$Path) {
        return $Path -and (Test-Path -LiteralPath (Join-Path $Path 'RELEASE.json') -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $Path 'runtime\python.exe') -PathType Leaf)
    }
    if (-not $Target) {
        $parent = Split-Path -Parent $PSScriptRoot
        if (Is-Package $parent) { $Target = $parent }
    }
    if (-not $Target) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shortcut = Join-Path $desktop '量禾混剪.lnk'
        if (Test-Path -LiteralPath $shortcut -PathType Leaf) {
            $ws = New-Object -ComObject WScript.Shell
            $link = $ws.CreateShortcut($shortcut)
            $candidate = Split-Path -Parent $link.TargetPath
            if (Is-Package $candidate) { $Target = $candidate }
        }
    }
    if (-not $Target -and $NonInteractive) { throw '非交互更新需要 -Target 指定完整安装目录。' }
    if (-not $Target) {
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = '请选择旧版量禾混剪.exe所在的完整软件文件夹（含runtime和app）'
        $dialog.ShowNewFolderButton = $false
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Write-Host '已取消，程序未修改。'; exit 0 }
        $Target = $dialog.SelectedPath
    }
    $Target = (Resolve-Path -LiteralPath $Target).Path
    if (-not (Is-Package $Target)) { throw '请选择完整文件夹版安装目录，不是源码或下载分块目录。' }
    $python = Join-Path $Target 'runtime\python.exe'
    $arguments = @('-I','-B','-X','utf8',(Join-Path $PSScriptRoot 'apply_update.py'),'--target',$Target)
    if (-not $NonInteractive) { $arguments += '--notify' }
    if ($Rollback) { $arguments += '--rollback' } else { $arguments += @('--bundle',$PSScriptRoot) }
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message
    if (-not $NonInteractive) { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message,'量禾混剪更新') | Out-Null }
    exit 1
}
