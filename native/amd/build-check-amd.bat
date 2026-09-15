@echo off
REM Compile-check the AMD driver units without linking them into the worker.
REM
REM The driver is not part of the worker build yet: it compiles standalone
REM (-c) so a syntax or type error is caught the moment it is written, not
REM on the day the AMD path is wired in.
REM
REM Usage: build-check-amd.bat   (run from native\amd\)

setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
    echo vcvars64 not found - adjust the path in this file
    exit /b 1
)

cd /d "%~dp0"
cl /nologo /c /std:c++17 /O2 /EHsc /W3 /MD amd_runtime.cpp
if errorlevel 1 (
    echo amd_runtime.cpp: FAILED
    exit /b 1
)
echo amd_runtime.cpp: OK
