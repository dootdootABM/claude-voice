@echo off
rem cv.cmd - Claude Voice launcher for Windows (cmd.exe). `cv --plain` for the text UI, `cv --help` for options.
rem Runs sidecar\supervisor.py, which restarts the app when a task asks for it (exit code 75). Untested on real Windows.
rem `cv ptt down|up|toggle` is handled by supervisor.py too (it talks to the running app instead of starting one).
setlocal
set "PYTHONUTF8=1"
set "SIDECAR=%~dp0..\sidecar"
set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Programs\uv;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install it from https://docs.astral.sh/uv/ and run cv again. 1>&2
    exit /b 1
)
uv run --quiet --no-sync --project "%SIDECAR%" python "%SIDECAR%\supervisor.py" %*
exit /b %ERRORLEVEL%
