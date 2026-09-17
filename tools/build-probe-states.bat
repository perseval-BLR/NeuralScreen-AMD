@echo off
rem probe_states.exe -- validates the frame's resource-state bookkeeping
rem against the D3D12 debug layer, which is the only authority on whether a
rem barrier is legal. Built and run beside the other probes.
cd /d "%~dp0.."
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [states] MSVC 2022 Build Tools not found.
    exit /b 1
)
rem Paths are relative to native\ (the compiler is run from there).
cd native
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 ..\tools\probe_states.cpp ^
   /Fe:probe_states.exe ^
   /link kernel32.lib d3d12.lib dxgi.lib
if errorlevel 1 exit /b 1
cd ..
endlocal
echo probe_states built: native\probe_states.exe
