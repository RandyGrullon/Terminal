@echo off
rem NEXUS Terminal - doble clic para arrancar todo (entorno, interfaz, base de datos y servidor).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo NEXUS se detuvo con un error. Revisa los mensajes de arriba.
  pause
)
endlocal
