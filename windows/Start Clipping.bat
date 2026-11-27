@echo off
REM Starts Clipping: the stream watcher and the Studio dashboard (opens in your browser).
REM Keep this window open while you work. Close it to stop everything.
cd /d "%~dp0.."
title Clipping - keep this window open while you work
python -m studio app
if errorlevel 1 (
  echo.
  echo Clipping stopped with an error. Send the text above to Claude.
)
pause
