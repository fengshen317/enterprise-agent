@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i not "%ProgramData%"=="C:\ProgramData" exit /b 1
set "TARGET=%ProgramData%\ssh\enterprise-agent-auto"
if not exist "%TARGET%\windows-entry.cmd" exit /b 1
if not exist "%TARGET%\receiver.sh" exit /b 1
if not exist "%~dp0receiver.sh" exit /b 1
set "BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH_EXE%" (
    for /f "delims=" %%G in ('where git.exe 2^>nul') do (
        if exist "%%~dpG..\bin\bash.exe" set "BASH_EXE=%%~dpG..\bin\bash.exe"
    )
)
if not exist "%BASH_EXE%" exit /b 1
mkdir "%TARGET%\deploy.lock" || exit /b 75
set "BACKUP=%TARGET%\receiver.previous-%RANDOM%%RANDOM%.sh"
set "STAGING=%TARGET%\receiver.staging-%RANDOM%%RANDOM%.sh"
if exist "%BACKUP%" goto :failed
if exist "%STAGING%" goto :failed
copy /b "%TARGET%\receiver.sh" "%BACKUP%" >nul || goto :failed
copy /b "%~dp0receiver.sh" "%STAGING%" >nul || goto :failed
set "BASH_ENV="
set "SCRIPT=%STAGING:\=/%"
"%BASH_EXE%" --noprofile --norc -n "%SCRIPT%" || goto :failed
move /y "%STAGING%" "%TARGET%\receiver.sh" >nul || goto :failed
rmdir "%TARGET%\deploy.lock" || exit /b 1
echo OK: Production receiver updated.
exit /b 0

:failed
rmdir "%TARGET%\deploy.lock"
echo FAIL: Production receiver update did not complete.
exit /b 1
