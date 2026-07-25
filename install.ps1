$ErrorActionPreference = "Stop"

$Installer = Join-Path $PSScriptRoot "install-trading-agent.ps1"
& $Installer @args
exit $LASTEXITCODE
