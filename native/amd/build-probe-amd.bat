@echo off
REM Build the AMD probe. Standalone: no NGX SDK, no project includes.
REM
REM The probe is the first thing to run on a Radeon machine - it answers
REM "are the files here, is this the known build, does HIP see the card"
REM before any of it is wired into the worker.
REM
REM Usage: build-probe-amd.bat   (run from native\amd\)

setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
    echo vcvars64 not found - adjust the path in this file
    exit /b 1
)

cd /d "%~dp0"
cl /nologo /std:c++17 /O2 /EHsc /W3 /MD probe_amd.cpp /Fe:probe_amd.exe ^
   /link bcrypt.lib advapi32.lib
if errorlevel 1 (
    echo build failed
    exit /b 1
)
echo built: %~dp0probe_amd.exe
