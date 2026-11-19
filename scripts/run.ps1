# Keeps the Discord assistant running: restarts on exit with backoff 5 s -> 5 min, reset after a healthy run.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1

# Continue, not Stop: Windows PowerShell turns native stderr lines into errors and would kill the loop.
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$RunLog = Join-Path $LogDir 'run.log'
$OutLog = Join-Path $LogDir 'stdout-stderr.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$MinDelay = 5
$MaxDelay = 300
$HealthySeconds = 600
$Delay = $MinDelay

function Write-RunLog([string]$Message) {
    $Line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message
    Add-Content -Path $RunLog -Value $Line -Encoding utf8
    Write-Host $Line
}

Set-Location $Root
while ($true) {
    $Started = Get-Date
    Write-RunLog "starting assistant"
    & $Python -I -X utf8 -m assistant 2>&1 | Out-File -FilePath $OutLog -Append -Encoding utf8
    $Code = $LASTEXITCODE
    $Ran = [int]((Get-Date) - $Started).TotalSeconds
    Write-RunLog "exited with code $Code after $Ran s"
    if ($Code -eq 2) {
        Write-RunLog 'setup error (exit 2): fix .env or enable Message Content and Server Members intents (Discord Developer Portal -> Bot), see logs\stdout-stderr.log; not restarting'
        exit 2
    }
    if ($Ran -ge $HealthySeconds) { $Delay = $MinDelay }
    Write-RunLog "restarting in $Delay s"
    Start-Sleep -Seconds $Delay
    $Delay = [Math]::Min($Delay * 2, $MaxDelay)
}
