@echo off
rem ============================================================
rem  PDF diff tool v5.0 - rebuild the Python environment
rem
rem  Use this only when the tool suddenly stops starting, or on
rem  the first run on a new computer.
rem
rem  WHY THERE IS NO PATH IN HERE
rem    "%~dp0" is the folder this .bat lives in, so it works
rem    wherever you copy the package.  Never type a path.
rem
rem  WHY THIS FILE IS PURE ASCII
rem    cmd.exe decodes .bat with the console codepage, which
rem    differs between machines.  All Chinese text is printed by
rem    Python (setup_env.py / bootui.py), never from here.
rem    That is also why there are no English banners here: one
rem    process prints the whole report, so it cannot drift.
rem
rem  No parenthesised blocks on purpose - "if errorlevel" inside a
rem  block is a classic source of surprises.
rem ============================================================
setlocal
pushd "%~dp0"

set "PYEXE="

rem ---- 1) prefer the official "py" launcher from python.org ----
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto RUN

rem ---- 2) fall back to "python" on PATH ----
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto RUN

rem ---- 3) no Python at all: the ONLY English block in the tool ----
echo.
echo   [!] Python 3 was not found on this computer.
echo.
echo       Install it from:
echo         https://www.python.org/downloads/windows/
echo.
echo       During setup, TICK this box:
echo         [x] Add python.exe to PATH
echo.
echo       Then double-click this file again.
echo.
pause
popd
exit /b 1

:RUN
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem --rebuild throws the venv away and builds it again; --setup stops there
rem instead of opening the window.  Everything else is reported by Python.
%PYEXE% "%~dp0setup_env.py" --rebuild --setup
set "RC=%ERRORLEVEL%"

echo.
pause
popd
exit /b %RC%
