$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $python)) {
    throw '请先运行 install.ps1，建立本软件独立环境。'
}
Start-Process -FilePath $python -ArgumentList @('-B', '-X', 'utf8', "`"$(Join-Path $PSScriptRoot 'app.py')`"") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
