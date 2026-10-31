<#
.SYNOPSIS
  Arranca NEXUS Terminal con un solo comando.

.DESCRIPTION
  1. Crea el entorno de Python (backend/.venv) e instala dependencias (solo cuando cambian).
  2. Instala y compila la interfaz web (solo cuando cambia el código).
  3. Levanta el servidor: crea/migra la base de datos local SQLite (data/nexus.db) y arranca
     todos los servicios (datos en vivo, grabador, vigilancia NEXUS, escáner, evaluador, cuenta demo).
  4. Abre el navegador cuando el servidor está listo.

.PARAMETER Dev
  Modo desarrollo: Vite con recarga en caliente en http://127.0.0.1:5173 + API en el puerto indicado.

.PARAMETER Port
  Puerto del servidor (por defecto 8420).

.PARAMETER NoBrowser
  No abrir el navegador automáticamente.

.PARAMETER Rebuild
  Fuerza la recompilación de la interfaz.

.EXAMPLE
  .\start.bat
  .\start.bat -Dev
  .\start.bat -Port 9000 -NoBrowser
  powershell -ExecutionPolicy Bypass -File .\start.ps1 -Dev

  start.bat pasa los argumentos a este script con -ExecutionPolicy Bypass: la política predeterminada de Windows
  (Restricted) no deja ejecutar .\start.ps1 directamente.
#>
param(
  [switch]$Dev,
  [int]$Port = 8420,
  [switch]$NoBrowser,
  [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Venv = Join-Path $Backend '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Uv = Get-Command uv -ErrorAction SilentlyContinue

function Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail([string]$msg) {
  Write-Host "ERROR: $msg" -ForegroundColor Red
  exit 1
}
function Get-TextHash([string]$text) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
  return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
}

Write-Host ''
Write-Host '  NEXUS Terminal' -ForegroundColor Green
Write-Host '  Terminal de investigación de mercados con IA adaptativa (local)' -ForegroundColor DarkGray
Write-Host ''

# ----------------------------------------------------------------------------- 1. Python
Step 'Comprobando Python'
if (-not (Test-Path $Py)) {
  Step 'Creando entorno virtual de Python (backend/.venv)'
  if ($Uv) {
    & uv venv $Venv --python 3.13
    if ($LASTEXITCODE -ne 0) { & uv venv $Venv }
  }
  elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { & py -3 -m venv $Venv }
  }
  elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m venv $Venv
  }
  else {
    Fail 'No se encontró Python 3.11 o superior. Instálalo desde https://www.python.org/downloads/ y vuelve a ejecutar.'
  }
  if (-not (Test-Path $Py)) { Fail 'No se pudo crear el entorno virtual de Python.' }
}

# requirements-dev.txt = dependencias del servidor (requirements.txt) + herramientas de pruebas (pytest)
$Req = Join-Path $Backend 'requirements.txt'
$ReqDev = Join-Path $Backend 'requirements-dev.txt'
$ReqHashFile = Join-Path $Venv '.req-hash'
$ReqHash = Get-TextHash ((Get-FileHash $Req -Algorithm SHA256).Hash + (Get-FileHash $ReqDev -Algorithm SHA256).Hash)
$Installed = if (Test-Path $ReqHashFile) { (Get-Content $ReqHashFile -Raw).Trim() } else { '' }
if ($Installed -ne $ReqHash) {
  Step 'Instalando dependencias de Python (solo cuando cambian)'
  $ok = $false
  if ($Uv) {
    # --system-certs usa el almacén de certificados de Windows (necesario detrás de proxies corporativos)
    & uv pip install --system-certs --python $Py -r $ReqDev
    if ($LASTEXITCODE -eq 0) { $ok = $true }
    else {
      & uv pip install --native-tls --python $Py -r $ReqDev
      if ($LASTEXITCODE -eq 0) { $ok = $true }
    }
  }
  if (-not $ok) {
    & $Py -m pip install --disable-pip-version-check -r $ReqDev
    if ($LASTEXITCODE -eq 0) { $ok = $true }
  }
  if (-not $ok) { Fail 'Falló la instalación de dependencias de Python.' }
  Set-Content -Path $ReqHashFile -Value $ReqHash -Encoding ascii
}

# ----------------------------------------------------------------------------- 2. Interfaz web
$Dist = Join-Path $Frontend 'dist\index.html'
$Node = Get-Command node -ErrorAction SilentlyContinue
if (-not $Node) {
  if (-not (Test-Path $Dist)) { Fail 'No se encontró Node.js 18+ (https://nodejs.org) y la interfaz no está compilada.' }
  Write-Warning 'Node.js no está instalado: se usará la interfaz ya compilada.'
}
else {
  $Lock = Join-Path $Frontend 'package-lock.json'
  $Modules = Join-Path $Frontend 'node_modules'
  $LockHashFile = Join-Path $Modules '.lock-hash'
  $LockHash = if (Test-Path $Lock) { (Get-FileHash $Lock -Algorithm SHA256).Hash } else { 'none' }
  $ModHash = if (Test-Path $LockHashFile) { (Get-Content $LockHashFile -Raw).Trim() } else { '' }
  if (-not (Test-Path $Modules) -or $ModHash -ne $LockHash) {
    Step 'Instalando dependencias de la interfaz (npm)'
    Push-Location $Frontend
    try {
      if (Test-Path $Lock) { & npm ci --no-audit --no-fund } else { & npm install --no-audit --no-fund }
      if ($LASTEXITCODE -ne 0) { Fail 'Falló npm install.' }
    }
    finally { Pop-Location }
    Set-Content -Path $LockHashFile -Value $LockHash -Encoding ascii
  }

  if (-not $Dev) {
    $SrcFiles = Get-ChildItem -Path (Join-Path $Frontend 'src') -Recurse -File | Sort-Object FullName
    $Stamp = ($SrcFiles | ForEach-Object { "$($_.FullName)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) -join "`n"
    foreach ($f in @('index.html', 'vite.config.ts', 'package.json', 'tsconfig.json')) {
      $p = Join-Path $Frontend $f
      if (Test-Path $p) { $Stamp += "`n" + (Get-FileHash $p -Algorithm SHA256).Hash }
    }
    $BuildHash = Get-TextHash $Stamp
    $BuildHashFile = Join-Path $Frontend 'dist\.build-hash'
    $Built = if (Test-Path $BuildHashFile) { (Get-Content $BuildHashFile -Raw).Trim() } else { '' }
    if ($Rebuild -or -not (Test-Path $Dist) -or $Built -ne $BuildHash) {
      Step 'Compilando la interfaz web'
      Push-Location $Frontend
      try {
        & npx vite build
        if ($LASTEXITCODE -ne 0) { Fail 'Falló la compilación de la interfaz.' }
      }
      finally { Pop-Location }
      Set-Content -Path $BuildHashFile -Value $BuildHash -Encoding ascii
    }
  }
}

# ----------------------------------------------------------------------------- 3. Servidor
$env:NEXUS_PORT = "$Port"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$ApiUrl = "http://127.0.0.1:$Port"
$Url = $ApiUrl

if ($Dev) {
  Step 'Modo desarrollo: iniciando Vite (http://127.0.0.1:5173)'
  $env:NEXUS_BACKEND = $ApiUrl
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm run dev' -WorkingDirectory $Frontend
  $Url = 'http://127.0.0.1:5173'
}

if (-not $NoBrowser) {
  # Abre el navegador cuando la API responde.
  Start-Job -ArgumentList $ApiUrl, $Url -ScriptBlock {
    param($api, $url)
    for ($i = 0; $i -lt 120; $i++) {
      try {
        $r = Invoke-WebRequest -Uri "$api/api/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { Start-Process $url; break }
      }
      catch { Start-Sleep -Milliseconds 500 }
    }
  } | Out-Null
}

Step "Iniciando NEXUS en $Url  (Ctrl+C para detener)"
Push-Location $Backend
try {
  & $Py -m app.main
}
finally {
  Pop-Location
  Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
}
