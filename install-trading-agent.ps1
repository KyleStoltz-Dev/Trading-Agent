$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
    }
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & py -3.12 -m venv .venv
    }
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
    }
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & python -m venv .venv
    }
}
else {
    throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -e ".[dev,ai]"
& ".venv\Scripts\trade.exe" setup
