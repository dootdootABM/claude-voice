@echo off
rem get-models.cmd - fetch the Kokoro text-to-speech files into sidecar\models\ (see sidecar\get_models.py).
rem `get-models --cpu` for a machine without CUDA; `get-models --from DIR` to take them from another copy.
setlocal
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
%PY% "%~dp0..\sidecar\get_models.py" %*
exit /b %ERRORLEVEL%
