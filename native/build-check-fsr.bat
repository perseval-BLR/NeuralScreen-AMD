@echo off
rem Compile-check the FidelityFX bridge on its own, so a broken header or a
rem mistyped FFX entry point is caught without a full worker build.
cd /d "%~dp0"
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /Iinclude /c amd\amd_fsr.cpp /Fo:amd_fsr_probe.obj
if errorlevel 1 exit /b 1
endlocal
echo fsr bridge compiled.
