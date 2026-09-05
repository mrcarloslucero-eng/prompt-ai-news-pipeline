# Self-elevate if not already running as admin
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$batPath = "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_news_brief.bat"

$action   = New-ScheduledTaskAction -Execute $batPath
$trigger  = New-ScheduledTaskTrigger -Daily -At "06:30AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable $true

Register-ScheduledTask -TaskName "AI News Brief" -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force

Write-Host "Task registered successfully. Press Enter to close."
Read-Host
