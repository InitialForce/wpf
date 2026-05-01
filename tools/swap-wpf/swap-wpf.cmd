@echo off
:: swap-wpf.cmd — thin wrapper that invokes swap-wpf.ps1 without requiring
::                the user to change their PowerShell execution policy.
::
:: Usage (from cmd or WSL):
::   swap-wpf.cmd swap    -ForkOutputDir <path> -McBuildDir <path>
::   swap-wpf.cmd restore -McBuildDir <path>
::   swap-wpf.cmd status  -McBuildDir <path>
::
:: From WSL:
::   cmd.exe /c "C:\path\to\swap-wpf.cmd swap -ForkOutputDir ... -McBuildDir ..."

setlocal
set SCRIPT_DIR=%~dp0
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%swap-wpf.ps1" %*
exit /b %ERRORLEVEL%
