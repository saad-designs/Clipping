@echo off
REM One-time setup for the Clipping Studio on Windows. Double-click this file.
setlocal
cd /d "%~dp0.."
echo.
echo === Clipping Studio setup ===
echo This installs Python and ffmpeg (if missing), then Clipping's Python packages.
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

REM Programs winget just installed (ffmpeg) are linked here; make them usable without reopening windows.
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
REM New installs only show up in a fresh window's PATH.
python --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was just installed. Close this window and double-click setup.bat once more.
  pause
  exit /b 0
)

echo Installing Clipping's packages (a few minutes; slow connections are retried automatically)...
python windows\install_packages.py
if errorlevel 1 (
  echo.
  echo Some packages are still missing. Run setup.bat again, or send the text above to Claude.
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo  Setup finished.
echo  Double-click "Start Clipping.bat". Your browser opens the dashboard.
echo  In the dashboard:
echo    1. Campaigns: add your campaign, paste the brief, add streamers
echo    2. Streamers: press "Check finished streams now"
echo  Clips appear in Review. Keep the window open while you work.
echo ==========================================================
pause
