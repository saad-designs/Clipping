@echo off
REM Leave this window open: it downloads clips you approve on the dashboard and edits them.
REM Finished clips appear in the Campaigns folder next to this repo.
cd /d "%~dp0.."
title Clipping Studio - leave open
python -m studio run
pause
