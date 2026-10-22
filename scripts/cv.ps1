# cv.ps1 - Claude Voice launcher for Windows (PowerShell). `cv --plain` for the text UI, `cv --help` for options.
# Runs sidecar\supervisor.py, which restarts the app when a task asks for it (exit code 75). Untested on real Windows.
# `cv ptt down|up|toggle` is handled by supervisor.py too (it talks to the running app instead of starting one).
$env:PYTHONUTF8 = "1"
$sidecar = Join-Path (Split-Path -Parent $PSScriptRoot) "sidecar"
$env:PATH = (Join-Path $env:USERPROFILE ".local\bin") + ";" + (Join-Path $env:LOCALAPPDATA "Programs\uv") + ";" + $env:PATH
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv was not found. Install it from https://docs.astral.sh/uv/ and run cv again."
    exit 1
}
& uv run --quiet --no-sync --project $sidecar python (Join-Path $sidecar "supervisor.py") @args
exit $LASTEXITCODE
