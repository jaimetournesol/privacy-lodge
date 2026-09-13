#!/usr/bin/env pwsh
# One implementation on every platform: Windows uses Docker Desktop's WSL2 integration.
param([string]$Command = 'help', [Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'Install WSL2 and enable Docker Desktop integration for its Linux distribution, then run this command again.'
}
$LinuxHere = (& wsl.exe --exec wslpath -a $Here | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or -not $LinuxHere) { throw 'Could not locate this installation inside WSL2.' }
# Arguments are separate native arguments. Never interpolate credentials or file names into bash code.
& wsl.exe --cd $LinuxHere --exec bash ./pl-box $Command @Rest
exit $LASTEXITCODE
