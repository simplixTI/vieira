$DIR = 'C:\Users\GalaxyBook3\Documents\Claude Code\tse_atual_2307\tse_atual_2307\export_vps'
$arg = '/c ""' + $DIR + '\run_crmlite.bat"" >> "' + $DIR + '\logs\agendador.log" 2>&1"'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 15) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName 'CRMLite-TSE-Worker' -Action $action -Trigger $trigger `
  -Description 'Worker TSE -> CRM Lite (a cada 15 min; AHK precisa de sessao grafica)' -Force | Out-Null
Disable-ScheduledTask -TaskName 'CRMLite-TSE-Worker' | Out-Null
Get-ScheduledTask -TaskName 'CRMLite-TSE-Worker' | Select-Object TaskName, State | Format-List
