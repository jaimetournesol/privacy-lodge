#!/usr/bin/env pwsh
# pl-box.ps1 - run and manage a Privacy Lodge box in Docker on Windows (native PowerShell).
# Mirrors ./pl-box (bash). Needs Docker Desktop. Full guide: docker/README.md.
#
#   .\pl-box.ps1 init            create .env (admin user/pass + a fresh secrets key)
#   .\pl-box.ps1 build           build the image (needs Linux-staged artifacts - see below)
#   .\pl-box.ps1 up              start the box (mints its onion on first run)
#   .\pl-box.ps1 qr              print the phone-connect QR
#   .\pl-box.ps1 status          running? onion, uptime
#   .\pl-box.ps1 logs            follow the logs
#   .\pl-box.ps1 restart | down  restart / stop (identity kept in the volume)
#   .\pl-box.ps1 update          rebuild + recreate on the same volume
#   .\pl-box.ps1 backup [dir]    tar the volume (onion key + secrets + pairings) - DO THIS
#   .\pl-box.ps1 restore <file>  restore a backup (box must be down)
#   .\pl-box.ps1 shell | destroy shell in the container / remove box + volume (asks first)

[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Command = 'help',
  [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = 'Stop'
# PS 7.4+ turns a non-zero native exit into a THROW under Stop - which would break our own
# `$LASTEXITCODE` checks (docker image/volume inspect exit 1 to mean "absent"). Opt out.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}
$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here                      # so `docker compose` uses this dir's file + .env
$Image  = if ($env:IMAGE) { $env:IMAGE } else { 'privacy-lodge-box:dev' }
# $Volume is resolved from .env (PL_VOLUME) just below, once Env-Val is defined.

function Die($m)        { Write-Host "error: $m" -ForegroundColor Red; exit 1 }
function Have-Env       { if (-not (Test-Path "$Here\.env")) { Die "no .env yet - run: .\pl-box.ps1 init" } }
function Image-Exists   { docker image inspect $Image *> $null; return ($LASTEXITCODE -eq 0) }
function Box-Running    { return ((docker ps --format '{{.Names}}' 2>$null) -contains 'privacy-lodge-box') }
function Volume-Exists  { docker volume inspect $Volume *> $null; return ($LASTEXITCODE -eq 0) }
function Env-Val($k) {
  if (-not (Test-Path "$Here\.env")) { return '' }
  $m = Select-String -Path "$Here\.env" -Pattern "^$k=" | Select-Object -First 1
  if ($m) { return ($m.Line -replace "^$k=", '') } else { return '' }
}
function Onion {
  $j = docker exec privacy-lodge-box sh -c 'cat /data/box.json 2>/dev/null' 2>$null
  if ($j -and ($j -match '"onion"[^"]*"([a-z2-7]+\.onion)"')) { return $Matches[1] } else { return '' }
}

# Per-install volume name. `init` writes a unique PL_VOLUME into .env so two boxes on one host
# cannot collide. CRITICAL: this volume holds the onion key, so it must resolve to the SAME
# name for the life of the box. Installs made before PL_VOLUME existed fall back to the
# original fixed name, so nothing is orphaned.
$Volume = Env-Val 'PL_VOLUME'
if (-not $Volume) { $Volume = 'privacy-lodge-data' }

switch ($Command) {

  'init' {
    if ((Test-Path "$Here\.env") -and ($Rest -notcontains '--force')) {
      Die ".env already exists (use: .\pl-box.ps1 init --force to overwrite - this rotates the secrets key!)"
    }
    $b = Read-Host "Box name [mybox]";             if (-not $b) { $b = 'mybox' }
    Write-Host "You can set your username + password in the browser after starting the box"
    Write-Host "(recommended). Or bake them in now for a scripted/non-interactive setup."
    $sec = Read-Host "Box admin password (leave blank to set it in the browser)" -AsSecureString
    $p = [System.Net.NetworkCredential]::new('', $sec).Password
    $u = ''
    if ($p) {
      # Scripted setup needs an explicit account name - there is no default.
      while (-not $u) { $u = Read-Host "Phone login username (required)" }
    }
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes)
    # Unique per-install volume name, generated ONCE here and pinned in .env. Two boxes on one
    # host cannot collide. It must never change afterwards - it holds the onion key.
    $vbytes = New-Object byte[] 4
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($vbytes)
    $vol = 'privacy-lodge-data-' + ((($vbytes | ForEach-Object { $_.ToString('x2') })) -join '')
    $Volume = $vol
    $body = @"
# Privacy Lodge box config - read automatically by docker compose. KEEP THIS PRIVATE.
# PL_SECRETS_KEY decrypts secrets.json; it must stay the SAME across restarts. If you lose
# it you cannot decrypt the box's secrets - back it up together with the volume.
# PL_VOLUME is THIS box's data volume (onion key + account + pairings). Never change it:
# a different name means a different (empty) box. Back this file up with your volume.
PL_USER=$u
PL_BOX=$b
PL_PASS=$p
PL_SECRETS_KEY=$key
PL_VOLUME=$vol
"@
    # UTF-8 without BOM - a BOM would corrupt the first line for docker compose.
    [System.IO.File]::WriteAllText("$Here\.env", $body, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "OK this box's data volume: $vol   (recorded in .env - keep it)"
    # A volume from an earlier install may still be on this host. Be explicit that this new box
    # will NOT adopt it (a different volume = a different, empty box) and how to reuse it on
    # purpose - silently pointing elsewhere is how people think they've lost their onion.
    docker volume inspect privacy-lodge-data *> $null
    if ($LASTEXITCODE -eq 0) {
      Write-Host ""
      Write-Host "  NOTE: a 'privacy-lodge-data' volume from an earlier install is still on this host."
      Write-Host "        This box will use '$vol' and leave that one untouched."
      Write-Host "        If you actually meant to keep using that older box, stop now and set"
      Write-Host "        PL_VOLUME=privacy-lodge-data in .env before running '.\pl-box.ps1 up'."
      Write-Host ""
    }
    if (Image-Exists) { Write-Host "OK wrote .env. Next: .\pl-box.ps1 up" }
    else              { Write-Host "OK wrote .env. Next: .\pl-box.ps1 build   (then: .\pl-box.ps1 up)" }
  }

  'build' {
    # The image bundles a LINUX box binary + sidecars that build.sh stages from a Linux host;
    # they aren't produced on Windows. If someone copied a Linux-built docker/bin + docker/
    # privacy-lodge here we can still `docker build`; otherwise explain the Linux/WSL2 route.
    if ((Test-Path "$Here\privacy-lodge") -and (Test-Path "$Here\bin")) {
      docker build -t $Image $Here
      if ($LASTEXITCODE -ne 0) { Die "docker build failed" }
      Write-Host "OK built $Image"
    } else {
      Die @"
Can't build the image on native Windows: it bundles a Linux box binary + sidecars that are
staged on a Linux host (they don't exist on Windows). Get the image one of these ways:
  * Build it under WSL2 (real Linux):  cd docker && ./pl-box build
  * Or build on any Linux host, then move it over:
        docker save $Image -o pl-box.tar   # on Linux
        docker load -i pl-box.tar          # here on Windows
  * Or push it to a registry on Linux and 'docker pull' here.
Then: .\pl-box.ps1 init ; .\pl-box.ps1 up
"@
    }
  }

  { $_ -in 'up', 'start' } {
    Have-Env
    docker compose up -d
    if (-not (Env-Val 'PL_PASS')) {
      Write-Host "OK box starting. Finish setup in your browser:"
      Write-Host ""
      Write-Host "      http://127.0.0.1:8470/"
      Write-Host ""
      Write-Host "  Choose a username + password there, then scan the QR with the Privacy Lodge"
      Write-Host "  phone app. The setup page closes itself once your phone signs in."
    } else {
      Write-Host "OK box starting. Watch it mint its onion + print the QR:  .\pl-box.ps1 logs"
      Write-Host "  once it's up:  .\pl-box.ps1 qr"
    }
  }

  'qr' {
    Have-Env
    $o = Onion; $u = Env-Val 'PL_USER'
    if (-not $o) { Die "no onion yet - the box is still minting it (give it a minute), or it isn't running (.\pl-box.ps1 up)" }
    # LOGIN/setup handoff - the phone pre-fills onion + user on its sign-in screen. NOT the
    # contact form (privacy-lodge:@user:onion), which a not-yet-signed-in phone stashes as a
    # pending contact and bounces back to login.
    $payload = "privacybolt://connect?hs=${o}&user=${u}"
    Write-Host "Scan this in the Privacy Bolt app to sign in (then type your box password):"
    Write-Host "  box: @${u}:${o}"
    docker exec privacy-lodge-box qrencode -t ANSIUTF8 $payload
    # Terminal QRs don't always camera-scan (line spacing / font). Also write a PNG that
    # reliably does - open connect-qr.png in any image viewer and scan that.
    docker exec privacy-lodge-box qrencode -o /tmp/pp-qr.png -s 8 -m 4 $payload 2>$null
    docker cp privacy-lodge-box:/tmp/pp-qr.png "$Here\connect-qr.png" 2>$null
    if (Test-Path "$Here\connect-qr.png") { Write-Host "If the code above won't scan, open  connect-qr.png  (in this folder) and scan that." }
  }

  'status' {
    if (-not (Box-Running)) { Write-Host "* box: not running   (start it: .\pl-box.ps1 up)"; break }
    $up = docker ps --filter name=privacy-lodge-box --format '{{.Status}}'
    $o = Onion; $u = Env-Val 'PL_USER'
    Write-Host "* box: running   ($up)"
    if ($o) { Write-Host "  user:  @${u}:${o}" } else { Write-Host "  user:  @${u}:<minting onion...>" }
    Write-Host "  volume: $Volume   (identity lives here - back it up: .\pl-box.ps1 backup)"
  }

  'logs'    { docker compose logs -f --tail=200 }
  'restart' { docker compose restart; Write-Host "OK restarted" }
  { $_ -in 'down', 'stop' } { docker compose down; Write-Host "OK stopped (identity kept in the $Volume volume)" }

  'update' {
    Have-Env
    & "$Here\pl-box.ps1" build
    docker compose up -d --force-recreate
    Write-Host "OK rebuilt + recreated on the same volume (same onion, same account)"
  }

  'backup' {
    if (-not (Volume-Exists)) { Die "no $Volume volume yet (has the box ever run?)" }
    $dir = if ($Rest -and $Rest[0]) { $Rest[0] } else { "$Here\backups" }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $dir = (Resolve-Path $dir).Path            # absolute - Docker Desktop needs it for -v
    $o = Onion; $tag = if ($o) { ($o -replace '\.onion$','').Substring(0, [Math]::Min(12, ($o -replace '\.onion$','').Length)) } else { 'box' }
    $n = 1; while (Test-Path "$dir\pl-box-$tag-$n.tgz") { $n++ }
    $name = "pl-box-$tag-$n.tgz"
    docker run --rm -v "${Volume}:/data:ro" -v "${dir}:/backup" alpine tar czf "/backup/$name" -C /data .
    if ($LASTEXITCODE -ne 0) { Die "backup failed" }
    Write-Host "OK backed up the box identity -> $dir\$name"
    Write-Host "  (this holds the onion KEY + secrets + pairings - store it somewhere safe + private)"
  }

  'restore' {
    $file = if ($Rest) { $Rest[0] } else { '' }
    if (-not $file -or -not (Test-Path $file)) { Die "usage: .\pl-box.ps1 restore <backup.tgz>" }
    if (Box-Running) { Die "stop the box first so the restore is clean:  .\pl-box.ps1 down" }
    docker volume create $Volume | Out-Null
    $full = (Resolve-Path $file).Path
    $dir = Split-Path -Parent $full; $base = Split-Path -Leaf $full
    $ans = Read-Host "Restore '$base' INTO $Volume (overwrites its current contents)? [y/N]"
    if ($ans -ne 'y') { Write-Host "aborted"; break }
    docker run --rm -v "${Volume}:/data" -v "${dir}:/backup" alpine sh -c "rm -rf /data/* /data/..?* 2>/dev/null; tar xzf '/backup/$base' -C /data"
    if ($LASTEXITCODE -ne 0) { Die "restore failed" }
    Write-Host "OK restored into $Volume. Start it:  .\pl-box.ps1 up"
  }

  'shell' {
    docker exec -it privacy-lodge-box bash
    if ($LASTEXITCODE -ne 0) { docker exec -it privacy-lodge-box sh }
  }

  'destroy' {
    Write-Host "This removes the box AND its $Volume volume - the onion key + account are GONE."
    $ans = Read-Host "Type the box name to confirm ('$(Env-Val 'PL_BOX')')"
    if ($ans -and ($ans -eq (Env-Val 'PL_BOX'))) {
      docker compose down -v 2>$null
      docker volume rm $Volume 2>$null
      Write-Host "OK box destroyed. (Your .env is kept - delete it by hand if you want it gone too.)"
    } else { Write-Host "aborted" }
  }

  default {
    # print the header comment block (lines starting with #, after the shebang)
    Get-Content $MyInvocation.MyCommand.Path | Select-Object -Skip 1 |
      Where-Object { $_ -match '^#' } | ForEach-Object { $_ -replace '^# ?', '' }
  }
}
