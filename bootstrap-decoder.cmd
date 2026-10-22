@echo off
setlocal
set "LMT_ROOT=%~dp0"
if exist "%~dp0.runtime\decoder\python\python.exe" (
  "%~dp0.runtime\decoder\python\python.exe" -I "%~dp0bootstrap-decoder.py" %*
  if errorlevel 1 exit /b 1
  exit /b 0
)
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; if(-not [Environment]::Is64BitProcess){throw '64-bit Windows required'}; $root=$env:LMT_ROOT; $m=Get-Content -LiteralPath (Join-Path $root 'decoder-assets.json') -Raw|ConvertFrom-Json; $a=@($m.assets|Where-Object {$_.name -eq 'python-3.12.10-embed-amd64.zip'}); if($a.Count -ne 1 -or $a[0].sha256 -notmatch '^[0-9a-f]{64}$'){throw 'Invalid Python manifest'}; $a=$a[0]; $d=Join-Path $root '.runtime/downloads'; New-Item -ItemType Directory -Path $d -Force|Out-Null; $p=Join-Path $d $a.name; if(-not(Test-Path -LiteralPath $p)){if($env:LMT_ASSET_DIR -and (Test-Path -LiteralPath (Join-Path $env:LMT_ASSET_DIR $a.name))){Copy-Item -LiteralPath (Join-Path $env:LMT_ASSET_DIR $a.name) -Destination $p}else{[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri $a.url -OutFile ($p+'.part'); Move-Item -LiteralPath ($p+'.part') -Destination $p}}; if(([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([IO.File]::ReadAllBytes($p)))).Replace('-','').ToLowerInvariant() -ne $a.sha256 -or (Get-Item -LiteralPath $p).Length -ne $a.size){throw 'Python checksum mismatch'}; $b=Join-Path $root '.runtime/bootstrap-python'; if(-not(Test-Path -LiteralPath $b)){Add-Type -AssemblyName System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory($p,$b)}"
if errorlevel 1 exit /b 1
"%~dp0.runtime\bootstrap-python\python.exe" -I "%~dp0bootstrap-decoder.py" %*
exit /b %errorlevel%
