@echo off
rem ============================================================
rem  PDF diff tool v5.0 - Windows launcher
rem
rem  This file is deliberately pure ASCII.  Batch files are decoded
rem  by cmd.exe with the console codepage, which differs between
rem  machines; all Chinese text is printed by setup_env.py, where
rem  the encoding is guaranteed.  So: never add Chinese here.
rem
rem  All layout (headers, [ OK ] markers, column width) also comes
rem  from Python - see bootui.py.  This file only finds Python and
rem  hands over, so every project's console looks the same.
rem
rem  TWO WAYS IN, ONE REPORT
rem    The package ships without .venv, so usually there is no
rem    bundled interpreter and we need the system one.
rem    Once .venv exists it is self-contained: it runs setup_env.py
rem    itself, so no system Python is needed any more - and the
rem    startup report still comes from the same place.
rem
rem  No parenthesised blocks are used on purpose: "if errorlevel"
rem  inside a block is a classic source of surprises.
rem ============================================================
setlocal
pushd "%~dp0"

set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "PYEXE="

rem ---- 1) a ready venv needs no system Python ----
if exist "%VENVPY%" goto RUN_VENV

rem ---- 2) otherwise look for the official "py" launcher ----
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto RUN_SYS

rem ---- 3) then for "python" on PATH ----
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto RUN_SYS

rem ---- 4) no Python at all: the ONLY English block in the tool ----
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

:RUN_VENV
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%VENVPY%" "%~dp0setup_env.py" %*
set "RC=%ERRORLEVEL%"
goto CHECK

:RUN_SYS
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
%PYEXE% "%~dp0setup_env.py" %*
set "RC=%ERRORLEVEL%"

:CHECK
if "%RC%"=="0" goto DONE
echo.
echo   [!] Stopped with exit code %RC%. See the messages above.
echo.
pause

:DONE
popd
exit /b %RC%
