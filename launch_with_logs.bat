@echo off
setlocal enabledelayedexpansion

:: Get the directory where this script is located
set SCRIPT_DIR=%~dp0

:: Define the executable path (built by PyInstaller)
set EXE_PATH=%SCRIPT_DIR%dist\ArchitectureValidatorPro.exe

:: Create a logs directory if it doesn't exist
if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

:: Generate a timestamp for the log filename
for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c%%a%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a%%b)
set TIMESTAMP=%mydate%_%mytime%

:: Define the log file path
set LOG_FILE=%SCRIPT_DIR%logs\ArchitectureValidatorPro_%TIMESTAMP%.log

:: Check if executable exists
if not exist "%EXE_PATH%" (
    echo.
    echo ERROR: Executable not found at: %EXE_PATH%
    echo.
    echo Please build the app first using PyInstaller:
    echo   pyinstaller ArchitectureValidatorPro.spec
    echo.
    pause
    exit /b 1
)

:: Launch the app and capture logs
echo.
echo ===================================================================
echo Starting ArchitectureValidatorPro
echo Executable: %EXE_PATH%
echo Log file: %LOG_FILE%
echo ===================================================================
echo.

:: Run the executable and tee output to both console and log file
"%EXE_PATH%" 2>&1 | tee "%LOG_FILE%"

echo.
echo ===================================================================
echo Application closed
echo Full log saved to: %LOG_FILE%
echo ===================================================================
echo.
pause
