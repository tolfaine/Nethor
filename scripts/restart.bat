@echo off
rem Redeploy Nethor: tests, then restart the bot via the run.ps1 loop. Args pass through (-SkipTests, -DryRun).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1" %*
set CODE=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %CODE%
