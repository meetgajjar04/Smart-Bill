@echo off
REM One-command Windows launcher for Smart Bill AI.
REM Usage: run.cmd          normal start
REM        run.cmd -Seed    (re)load demo data before starting
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
endlocal
