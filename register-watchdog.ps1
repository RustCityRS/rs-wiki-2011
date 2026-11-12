# Registers (or replaces) the per-user "RS wiki watchdog" task: watchdog.ps1
# at logon and every 5 minutes, without a window. Remove it with:
#   Unregister-ScheduledTask -TaskName 'RS wiki watchdog' -Confirm:$false

$script = Join-Path $PSScriptRoot 'watchdog.ps1'
$pwsh = (Get-Command pwsh).Source
$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless `"$pwsh`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'RS wiki watchdog' -Action $action -Trigger @($logon, $every) `
    -Settings $settings -Principal $principal -Force | Select-Object TaskName, State
