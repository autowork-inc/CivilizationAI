<#
.SYNOPSIS
  One command: the Mini App backend + a public HTTPS tunnel (+ optionally the Telegram bot).

.DESCRIPTION
  1. builds the Svelte app (web\dist) if it is missing (needs Node; without it the backend serves a plain smoke page);
  2. starts `python -m ai_civ.web` and waits until /healthz answers;
  3. opens a tunnel to it: cloudflared (free quick tunnel, no account) or ngrok - whichever is installed;
  4. prints the public URL (and copies it to the clipboard). Put it into @BotFather as the Web App URL of the app.
  Ctrl+C stops everything it started.

.EXAMPLE
  .\scripts\dev.ps1                 # web + tunnel
  .\scripts\dev.ps1 -WithBot        # ... and the Telegram bot too
  .\scripts\dev.ps1 -Rebuild        # rebuild the frontend first
  .\scripts\dev.ps1 -Tunnel ngrok -NgrokDomain my-name.ngrok-free.app   # a STABLE address (ngrok static domain)

.NOTES
  A cloudflared quick tunnel gets a NEW address on every start, so the URL in @BotFather must be updated each time.
  For a permanent address use an ngrok static domain (-NgrokDomain) or a named Cloudflare tunnel.
#>
param(
  [switch]$WithBot,
  [switch]$Rebuild,
  [switch]$NoClipboard,
  [ValidateSet('auto', 'cloudflared', 'ngrok')][string]$Tunnel = 'auto',
  [string]$NgrokDomain = '',
  [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
# tools installed by winget in this very session are not on this process's PATH yet
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "No virtual environment at $python. Create it: python -m venv .venv ; .\.venv\Scripts\pip install -e '.[dev]'" }

if ($Port -eq 0) {
  $Port = 8080
  if ($env:WEB_PORT) { $Port = [int]$env:WEB_PORT }
  elseif (Test-Path '.env') {
    $hit = Select-String -Path '.env' -Pattern '^\s*WEB_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($hit) { $Port = [int]$hit.Matches[0].Groups[1].Value }
  }
}
$env:WEB_PORT = "$Port"

$runDir = Join-Path $root '.run'
New-Item -ItemType Directory -Force $runDir | Out-Null
$children = New-Object System.Collections.ArrayList

function Start-Child($name, $file, $arguments) {
  $out = Join-Path $runDir "$name.out.log"
  $err = Join-Path $runDir "$name.err.log"
  $p = Start-Process -FilePath $file -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
  [void]$children.Add($p)
  return $p
}

function Show-LogTail($name) {
  foreach ($suffix in 'err', 'out') {
    $path = Join-Path $runDir "$name.$suffix.log"
    if (Test-Path $path) { Write-Host "--- $name.$suffix.log ---" -ForegroundColor DarkGray; Get-Content $path -Tail 15 }
  }
}

function Stop-All {
  foreach ($p in $children) {
    if ($p -and -not $p.HasExited) { & taskkill /T /F /PID $p.Id 2>$null | Out-Null }  # /T: the venv launcher has a child
  }
}

try {
  # ---- 1. frontend
  $index = Join-Path $root 'web\dist\index.html'
  if ($Rebuild -or -not (Test-Path $index)) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
      Write-Host 'Building the Mini App (web\dist) ...' -ForegroundColor Cyan
      Push-Location (Join-Path $root 'web')
      try {
        if (-not (Test-Path 'node_modules')) { & npm install --no-audit --no-fund }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
      } finally { Pop-Location }
    } else {
      Write-Host 'Node/npm not found: the backend will serve its plain smoke page instead of the Mini App.' -ForegroundColor Yellow
    }
  }

  # ---- 2. backend
  Write-Host "Starting the backend on 127.0.0.1:$Port ..." -ForegroundColor Cyan
  $web = Start-Child 'web' $python '-m ai_civ.web'
  $up = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if ($web.HasExited) { Show-LogTail 'web'; throw 'The backend exited during start-up (see the log above).' }
    try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $up = $true; break } } catch { }
  }
  if (-not $up) { Show-LogTail 'web'; throw 'The backend did not answer /healthz in 20 s.' }

  # ---- 3. tunnel
  $kind = $Tunnel
  if ($kind -eq 'auto') {
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) { $kind = 'cloudflared' }
    elseif (Get-Command ngrok -ErrorAction SilentlyContinue) { $kind = 'ngrok' }
    else { throw 'Neither cloudflared nor ngrok is installed. Install one: winget install Cloudflare.cloudflared' }
  }
  Write-Host "Opening a $kind tunnel ..." -ForegroundColor Cyan
  $publicUrl = $null
  if ($kind -eq 'cloudflared') {
    $tun = Start-Child 'tunnel' 'cloudflared' "tunnel --no-autoupdate --url http://127.0.0.1:$Port"
    for ($i = 0; $i -lt 80 -and -not $publicUrl; $i++) {
      Start-Sleep -Milliseconds 500
      if ($tun.HasExited) { Show-LogTail 'tunnel'; throw 'cloudflared exited (see the log above).' }
      $log = (Get-Content (Join-Path $runDir 'tunnel.err.log') -Raw -ErrorAction SilentlyContinue)
      if ($log -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $publicUrl = $Matches[0] }
    }
  } else {
    $argList = "http $Port --log=stdout"
    if ($NgrokDomain) { $argList = "http $Port --url=$NgrokDomain --log=stdout" }
    $tun = Start-Child 'tunnel' 'ngrok' $argList
    for ($i = 0; $i -lt 60 -and -not $publicUrl; $i++) {
      Start-Sleep -Milliseconds 500
      if ($tun.HasExited) { Show-LogTail 'tunnel'; throw 'ngrok exited (see the log above). Did you run `ngrok config add-authtoken ...`?' }
      try {
        $api = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
        $https = $api.tunnels | Where-Object { $_.public_url -like 'https://*' } | Select-Object -First 1
        if ($https) { $publicUrl = $https.public_url }
      } catch { }
    }
  }
  if (-not $publicUrl) { Show-LogTail 'tunnel'; throw 'The tunnel did not report a public URL in 40 s.' }

  # the address is announced before DNS knows it (up to a minute for a fresh quick tunnel): wait until the page really answers
  Write-Host 'Waiting for the new address to become reachable (up to 90 s) ...' -ForegroundColor DarkGray
  $reachable = $false
  for ($i = 0; $i -lt 45 -and -not $reachable; $i++) {
    try { $reachable = (Invoke-WebRequest "$publicUrl/healthz" -UseBasicParsing -TimeoutSec 4).StatusCode -eq 200 } catch { Start-Sleep -Seconds 2 }
  }

  # ---- 4. bot (optional)
  if ($WithBot) {
    Write-Host 'Starting the Telegram bot ...' -ForegroundColor Cyan
    [void](Start-Child 'bot' $python '-m ai_civ.bot')
  }

  $direct = (& $python -c "from ai_civ.config import get_settings; print(get_settings().mini_app_direct_link)").Trim()
  $copied = ''
  if (-not $NoClipboard) { Set-Clipboard -Value $publicUrl; $copied = ' (copied to the clipboard)' }
  Write-Host ''
  Write-Host '================================================================' -ForegroundColor Green
  Write-Host "  Mini App URL${copied}:  $publicUrl" -ForegroundColor Green
  if (-not $reachable) { Write-Host '  (not reachable from this PC yet: DNS may need a little longer; Telegram resolves it on its own)' -ForegroundColor Yellow }
  Write-Host '================================================================' -ForegroundColor Green
  Write-Host '  1. @BotFather -> /myapps -> your bot -> the app -> Edit Web App URL -> paste the URL above'
  Write-Host "  2. Open the app:  $direct   (or the button in the Settings topic / the /app command)"
  Write-Host "  Logs: $runDir      Stop everything: Ctrl+C"
  if ($kind -eq 'cloudflared') { Write-Host '  This address changes on every start: update it in @BotFather each time.' -ForegroundColor Yellow }
  Write-Host ''

  while ($true) {
    Start-Sleep -Seconds 2
    foreach ($p in $children) {
      if ($p.HasExited) { Write-Host "A child process (pid $($p.Id)) exited. Logs: $runDir" -ForegroundColor Red; throw 'stopping' }
    }
  }
} finally {
  Write-Host 'Stopping ...' -ForegroundColor DarkGray
  Stop-All
}
