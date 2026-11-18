@echo off
rem Drag your Medal project folder (the one with project.json) onto this file,
rem or double-click it from inside that folder. Extra options go after the folder.
setlocal
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=%CD%"
set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% "%~dp0medal_project_render.py" "%TARGET%" %2 %3 %4 %5 %6 %7 %8 %9
echo.
pause
