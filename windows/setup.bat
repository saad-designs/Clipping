@echo off
REM One-time setup for the Clipping Studio on Windows. Double-click this file.
setlocal
cd /d "%~dp0.."
echo.
echo === Clipping Studio setup ===
echo This installs Python, ffmpeg and Git (if missing), then the Studio's Python packages.
echo.

where winget >nul 2>nul
if errorlevel 1 (
  echo winget is missing. Install "App Installer" from the Microsoft Store, then run this again.
  pause
  exit /b 1
)

REM "where python" is fooled by the Microsoft Store shortcut, so actually run it.
python --version >nul 2>nul
if errorlevel 1 (
  echo Installing Python...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
)
where git >nul 2>nul
if errorlevel 1 (
  echo Installing Git...
  winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
)

REM New installs only show up in a fresh window's PATH.
python --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was just installed. Close this window and double-click setup.bat once more.
  pause
  exit /b 0
)

echo Installing Studio packages (this can take a few minutes)...
python -m pip install --upgrade pip
python -m pip install -r requirements-studio.txt -r requirements-watcher.txt
if errorlevel 1 (
  echo.
  echo Package install failed. Send the text above to Claude.
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo  Setup finished.
echo  Double-click "Start Clipping.bat". Your browser opens the dashboard.
echo  In the dashboard:
echo    1. Settings: paste your Twitch Client ID and Secret
echo    2. Campaigns: add your campaign, paste the brief, add streamers
echo  Keep the window open while your streamers are live.
echo ==========================================================
pause
