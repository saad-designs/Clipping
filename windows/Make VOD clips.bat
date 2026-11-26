@echo off
REM Pick the best moments from a finished stream, podcast or long video.
cd /d "%~dp0.."
set /p URL="Paste the VOD/video link and press Enter: "
set /p CAMPAIGN="Campaign name (folder name, e.g. energy-drink): "
python -m studio vod "%URL%" --campaign "%CAMPAIGN%" --top 8
echo.
echo Done. Your clips are in Campaigns\%CAMPAIGN%\ready
pause
