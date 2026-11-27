@echo off
REM Checks ffmpeg, the video encoder, captions (whisper) and the connection to your server.
cd /d "%~dp0.."
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
python -m studio check
echo.
echo Every line should say ok (the encoder line names your graphics card's encoder, or libx264).
pause
