@echo off
setlocal
set "VSLANG=1033"
set "BRIDGE_ROOT=%~dp0.."
set "BRIDGE_MODE=full"
set "BRIDGE_REFERENCE=%~1"
if /I "%~1"=="--host-only" (
    set "BRIDGE_MODE=host"
    set "BRIDGE_REFERENCE="
)
if "%~1"=="" (
    echo Usage: %~nx0 --host-only
    echo        %~nx0 "path\to\prepared\obs-reference"
    exit /b 1
)
where cl.exe >nul 2>nul
if errorlevel 1 goto setup_msvc
goto tool_checks
:setup_msvc
set "BRIDGE_VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%BRIDGE_VSWHERE%" (
    echo Visual Studio Build Tools with Desktop development with C++ is required.
    exit /b 2
)
for /f "usebackq delims=" %%I in (`"%BRIDGE_VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "BRIDGE_VS_ROOT=%%I"
if not defined BRIDGE_VS_ROOT (
    echo vswhere found no MSVC x64 build environment.
    exit /b 2
)
call "%BRIDGE_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 2
:tool_checks
where cmake.exe >nul 2>nul
if errorlevel 1 (
    echo CMake 3.28 or newer is required on PATH.
    exit /b 2
)
where ninja.exe >nul 2>nul
if errorlevel 1 (
    echo Ninja is required on PATH.
    exit /b 2
)
if /I "%BRIDGE_MODE%"=="host" (
    cmake.exe -S "%BRIDGE_ROOT%\obs_plugin" -B "%BRIDGE_ROOT%\obs_plugin\build-host" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBRIDGE_HOST_TEST_ONLY=ON
) else (
    cmake.exe -S "%BRIDGE_ROOT%\obs_plugin" -B "%BRIDGE_ROOT%\obs_plugin\build-full" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBRIDGE_HOST_TEST_ONLY=OFF "-DOBS_REFERENCE=%BRIDGE_REFERENCE%"
)
if errorlevel 1 exit /b 3
cmake.exe --build "%BRIDGE_ROOT%\obs_plugin\build-%BRIDGE_MODE%"
if errorlevel 1 exit /b 4
ctest.exe --test-dir "%BRIDGE_ROOT%\obs_plugin\build-%BRIDGE_MODE%" --output-on-failure
exit /b %errorlevel%
