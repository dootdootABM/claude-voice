@echo off
rem install-cv.cmd - make `cv` work from any terminal on Windows, as linking scripts/cv into ~/.local/bin does on Linux.
rem Writes a small cv.cmd into %USERPROFILE%\.local\bin (where uv installs itself) that runs this copy's scripts\cv.cmd,
rem and adds that folder to the user PATH if it isn't there. `install-cv --remove` takes the cv.cmd away again.
setlocal
set "BIN=%USERPROFILE%\.local\bin"
set "SHIM=%BIN%\cv.cmd"
set "MARK=rem claude-voice shim"
for %%I in ("%~dp0cv.cmd") do set "TARGET=%%~fI"

if exist "%SHIM%" (
    findstr /b /c:"%MARK%" "%SHIM%" >nul || (
        echo %SHIM% exists and wasn't written by install-cv; leaving it alone. 1>&2
        exit /b 1
    )
)

if /i "%~1"=="--remove" (
    if exist "%SHIM%" del "%SHIM%"
    echo Removed %SHIM%.
    exit /b 0
)

if not exist "%BIN%" mkdir "%BIN%"
> "%SHIM%" (
    echo @echo off
    echo %MARK% ^(scripts\install-cv.cmd^): runs %TARGET%
    echo "%TARGET%" %%*
)

rem The user PATH, not this window's PATH: a new terminal reads it from the registry. Read and write it raw, keeping
rem entries like %USERPROFILE%\... unexpanded (Environment.SetEnvironmentVariable would write them back expanded), then
rem tell Windows it changed so new terminals see it.
powershell -NoProfile -Command "$b = $env:BIN; $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment'); $p = [string]$k.GetValue('Path', '', 'DoNotExpandEnvironmentNames'); $have = $p -split ';' | Where-Object { $_ } | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') }; if ($have -notcontains $b.TrimEnd('\')) { $k.SetValue('Path', ((@($p.TrimEnd(';')) + $b) -join ';').TrimStart(';'), 'ExpandString'); [Environment]::SetEnvironmentVariable('CV_INSTALL_REFRESH', '1', 'User'); [Environment]::SetEnvironmentVariable('CV_INSTALL_REFRESH', $null, 'User'); 'Added ' + $b + ' to your user PATH.' }; $k.Close()"
echo Installed %SHIM% -^> %TARGET%
echo Open a new terminal and run: cv
exit /b 0
