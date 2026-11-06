@echo off
rem NEXUS Terminal - doble clic para arrancar todo (entorno, interfaz, base de datos y servidor).
setlocal
cd /d "%~dp0"
rem Lanzado desde PowerShell 7, el PSModulePath heredado apunta a sus modulos y Windows PowerShell 5.1 no
rem encuentra cmdlets como Get-FileHash. Vacio, 5.1 reconstruye sus rutas por defecto (solo dentro de este setlocal).
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo NEXUS se detuvo con un error. Revisa los mensajes de arriba.
  pause
)
endlocal
