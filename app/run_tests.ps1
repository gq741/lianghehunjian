$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s tests -v
exit $LASTEXITCODE
