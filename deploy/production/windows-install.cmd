@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i not "%ProgramData%"=="C:\ProgramData" exit /b 1
set "TARGET=%ProgramData%\ssh\enterprise-agent-auto"
set "KEYS=%ProgramData%\ssh\administrators_authorized_keys"
if not exist "%KEYS%" (
    echo FAIL: Administrator authorized_keys was not found.
    exit /b 1
)
findstr /l /c:"enterprise-agent-production-deploy" "%KEYS%" >nul
if not errorlevel 1 (
    echo FAIL: A production deployment key already exists.
    exit /b 1
)
if exist "%TARGET%" (
    echo FAIL: A production receiver directory already exists.
    exit /b 1
)
if not exist "%~dp0authorized-key.txt" exit /b 1
if not exist "%ProgramFiles%\Git\bin\bash.exe" (
    where git.exe >nul 2>&1 || exit /b 1
)
mkdir "%TARGET%" || exit /b 1
icacls "%TARGET%" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" >nul || exit /b 1
for %%F in (windows-entry.cmd receiver.sh base-fingerprint.sh) do (
    copy /y "%~dp0%%F" "%TARGET%\%%F" >nul || exit /b 1
)
>>"%KEYS%" echo.
type "%~dp0authorized-key.txt" >>"%KEYS%" || exit /b 1
echo OK: Protected production deployment receiver installed.
