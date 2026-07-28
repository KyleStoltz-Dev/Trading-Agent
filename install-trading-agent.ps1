param(
    [switch]$NoSetup
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
    }
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Trading Agent virtual environment."
        }
    }
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
    }
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Trading Agent virtual environment."
        }
    }
}
else {
    throw "Python 3.12 or newer is required: https://www.python.org/downloads/windows/"
}

& ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv uses Python older than 3.12; replace that environment first."
}
& ".venv\Scripts\python.exe" -m pip install --require-hashes --only-binary=:all: `
    --requirement requirements-bootstrap.txt
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the verified uv bootstrap wheel."
}
& ".venv\Scripts\uv.exe" sync --locked --inexact --extra ai --extra metatrader
if ($LASTEXITCODE -ne 0) {
    throw "Could not synchronize the locked Trading Agent environment."
}
if (-not $NoSetup) {
    & ".venv\Scripts\trade.exe" setup
    if ($LASTEXITCODE -ne 0) {
        throw "Trading Agent setup did not complete."
    }
}
else {
    Write-Host "Locked Trading Agent environment installed; guided setup was skipped."
}
