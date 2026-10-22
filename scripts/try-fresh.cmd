@echo off
rem try-fresh.cmd - run this copy of Claude Voice as a brand-new user would: a throwaway config and state folder,
rem so your real config (%APPDATA%\claude-voice) and saved tasks are never read or touched. Arguments go to cv.
rem `try-fresh --keep` reuses the last throwaway folder instead of starting clean.
setlocal
set "BASE=%TEMP%\claude-voice-fresh"
set "CLAUDE_VOICE_CONFIG=%BASE%\config.yaml"
set "CLAUDE_VOICE_STATE_DIR=%BASE%\state"
if /i "%~1"=="--keep" goto keep
if exist "%BASE%" rmdir /s /q "%BASE%"
mkdir "%BASE%\state"
echo try-fresh: config %CLAUDE_VOICE_CONFIG%, state %CLAUDE_VOICE_STATE_DIR%
call "%~dp0cv.cmd" %*
exit /b %ERRORLEVEL%
:keep
if not exist "%BASE%\state" mkdir "%BASE%\state"
echo try-fresh: config %CLAUDE_VOICE_CONFIG%, state %CLAUDE_VOICE_STATE_DIR%
call "%~dp0cv.cmd" %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
