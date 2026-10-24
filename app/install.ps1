$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.12 x64。安装后重试。' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
Write-Output '环境安装完成。FFmpeg/ffprobe需可在PATH中找到，桌面窗口需要WebView2 Runtime。'
