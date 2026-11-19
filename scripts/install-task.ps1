# Registers the Task Scheduler task "Nethor": starts scripts\run.ps1 hidden at logon of the current user.
# Run once from a normal (non-admin is fine) PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName Nethor -Confirm:$false

$ErrorActionPreference = 'Stop'
$TaskName = 'Nethor'
$Root = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $Root 'scripts\run.ps1'
$User = "$env:USERDOMAIN\$env:USERNAME"

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
# No execution time limit; the run.ps1 loop handles restarts, Task Scheduler restarts the loop itself.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description 'Personal Discord assistant (scripts\run.ps1)' -Force | Out-Null
Write-Host "Registered task '$TaskName' for $User. Start now with: Start-ScheduledTask -TaskName $TaskName"
