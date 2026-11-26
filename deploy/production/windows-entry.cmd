@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH_EXE%" (
    for /f "delims=" %%G in ('where git.exe 2^>nul') do (
        if exist "%%~dpG..\bin\bash.exe" set "BASH_EXE=%%~dpG..\bin\bash.exe"
    )
)
if not exist "%BASH_EXE%" (
    echo FAIL: Git for Windows Bash was not found.
    exit /b 1
)
set "BASH_ENV="
set "RECEIVER=%~dp0receiver.sh"
set "RECEIVER=%RECEIVER:\=/%"
"%BASH_EXE%" --noprofile --norc "%RECEIVER%"
exit /b %errorlevel%
