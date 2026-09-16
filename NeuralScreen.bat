@echo off
rem NeuralScreen - DLSS 5 Neural Rendering overlay for the Windows desktop.
rem Consoles stays open: main.py logs FPS and pipeline timings into it.
setlocal
cd /d "%~dp0"

rem --- Python: env var -> bundled runtime -> PATH -----------------------------
set "NS_PY=%NEURALSCREEN_PYTHON%"
if not defined NS_PY if exist "%~dp0runtime\python.exe" set "NS_PY=%~dp0runtime\python.exe"
if not defined NS_PY set "NS_PY=python"

rem --- NGX runtime: 165 MB redistributable, ships in the release archive -----
if not exist "%~dp0native\nvngx_dlssnr.dll" (
    echo [NeuralScreen AMD] native\nvngx_dlssnr.dll not found.
    echo Re-download the release archive, or see README.md, section "What you need".
    pause
    exit /b 1
)

rem --- Worker: build artefact, not stored in git ----------------------------
if not exist "%~dp0native\nvngx.dll" (
    echo [NeuralScreen AMD] native\nvngx.dll not found - building it.
    call "%~dp0native\build-host.bat"
    if errorlevel 1 (
        echo [NeuralScreen AMD] Worker build failed. See native\build-host.bat.
        pause
        exit /b 1
    )
)

"%NS_PY%" -u "%~dp0main.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [NeuralScreen AMD] exit code %RC%
    pause
)
exit /b %RC%
