@echo off
REM Opens the Clipping Studio dashboard in your browser (http://localhost:8790).
REM Leave this window open while you work: it edits clips in the background.
cd /d "%~dp0.."
title Clipping Studio - leave this window open
python -m studio app
if errorlevel 1 (
  echo.
  echo Studio stopped with an error. Send the text above to Claude.
)
pause
