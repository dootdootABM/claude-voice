@echo off
rem get-models.cmd - fetch the Kokoro text-to-speech files into sidecar\models\ and the Whisper models into the
rem Hugging Face cache (see sidecar\get_models.py). `get-models --cpu` for a machine without CUDA;
rem `get-models --from DIR` to take the Kokoro files from another copy.
rem Runs in the uv environment (sidecar\.venv, from `uv sync`) when there is one: the Whisper download needs it.
setlocal
set "SIDECAR=%~dp0..\sidecar"
set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Programs\uv;%PATH%"
if not exist "%SIDECAR%\.venv\Scripts\python.exe" goto system
where uv >nul 2>nul
if errorlevel 1 goto system
uv run --quiet --no-sync --project "%SIDECAR%" python "%SIDECAR%\get_models.py" %*
exit /b %ERRORLEVEL%
:system
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
%PY% "%SIDECAR%\get_models.py" %*
exit /b %ERRORLEVEL%
