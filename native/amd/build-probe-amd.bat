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

REM The release archive ships native\probe_amd.exe (next to the worker, where a
REM user runs it from), while this script builds into native\amd\. Without this
REM copy the archive silently packs the PREVIOUS build - which is exactly what
REM happened once: a probe that did not know the new runtime image was shipped
REM with a build that did.
copy /Y probe_amd.exe "..\probe_amd.exe" >nul
if errorlevel 1 (
    echo warning: could not copy the probe to native\probe_amd.exe
    exit /b 1
)
echo copied: %~dp0..\probe_amd.exe  (what the archive ships)
