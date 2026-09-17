@echo off
rem probe_fsr.exe -- checks the FidelityFX bridge without a Radeon.
rem
rem The neural runtime needs HIP and a Radeon, so it cannot be exercised on an
rem NVIDIA bench. The dispatch it follows can: FSR is cross-vendor. This builds
rem that check and runs it, which is the only way to know the bridge is sound
rem before a user with a real card tries it.
cd /d "%~dp0.."
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [fsr] MSVC 2022 Build Tools not found.
    exit /b 1
)
rem Paths are relative to native\ (the compiler is run from there).
cd native
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /Iinclude ..\tools\probe_fsr.cpp amd\amd_fsr.cpp ^
   /Fe:probe_fsr.exe ^
   /link kernel32.lib d3d12.lib dxgi.lib
if errorlevel 1 exit /b 1
cd ..
endlocal
echo probe_fsr built: native\probe_fsr.exe
