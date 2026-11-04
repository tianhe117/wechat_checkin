$ErrorActionPreference = 'Stop'
$controllerRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $controllerRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create controller/.venv and install controller/requirements.txt first.'
}
& $pythonPath -X utf8 (Join-Path $controllerRoot 'main.py') @args
exit $LASTEXITCODE
