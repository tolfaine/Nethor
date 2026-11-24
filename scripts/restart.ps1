# Redeploys the Discord bot: runs the tests, stops the bot's python so the run.ps1 loop restarts it on the new code,
# then waits for "logged in" in logs\assistant.log. Starts the loop if it isn't running. Leaves --console sessions alone.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart.ps1 [-SkipTests] [-DryRun]
param([switch]$SkipTests, [switch]$DryRun)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$AppLog = Join-Path $Root 'logs\assistant.log'

function Get-BotProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$Python*-m assistant*" -and $_.CommandLine -notlike '*--console*' }
}

function Get-LoopProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' or Name = 'pwsh.exe'" |
        Where-Object { $_.CommandLine -like '*scripts\run.ps1*' }
}

function Wait-LoggedIn([datetime]$Since, [int]$TimeoutSeconds = 90) {
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Seconds 2
        $Hit = Select-String -Path $AppLog -Pattern 'assistant\.bot: logged in as' | Select-Object -Last 1
        if ($Hit) {
            $Stamp = [datetime]::ParseExact($Hit.Line.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null)
            if ($Stamp -ge $Since.AddSeconds(-1)) { return $Hit.Line }
        }
    }
    return $null
}

Set-Location $Root
if (-not $SkipTests) {
    Write-Host 'Running tests...'
    & $Python -I -X utf8 -m pytest -q
    if ($LASTEXITCODE -ne 0) { Write-Host 'Tests failed: not restarting.'; exit 1 }
}

$Loop = @(Get-LoopProcess)
$Bot = @(Get-BotProcesses)
Write-Host ("run.ps1 loop: {0} | bot python: {1}" -f ($Loop.ProcessId -join ','), ($Bot.ProcessId -join ','))
if ($DryRun) { Write-Host 'Dry run: nothing stopped or started.'; exit 0 }

$Since = Get-Date
if ($Loop.Count -eq 0) {
    Write-Host 'No run.ps1 loop found: starting it hidden.'
    $Bot | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
    $RunScript = Join-Path $PSScriptRoot 'run.ps1'
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $RunScript
} elseif ($Bot.Count -eq 0) {
    Write-Host 'Loop is running but no bot python (probably in restart backoff): it will start on its own.'
} else {
    $Bot | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
    Write-Host 'Stopped the bot; run.ps1 restarts it in ~5 s.'
}

$Line = Wait-LoggedIn -Since $Since
if ($Line) { Write-Host "OK: $Line"; exit 0 }
Write-Host 'Bot did not log in within 90 s: check logs\run.log and logs\stdout-stderr.log.'
exit 1
