# Run once as Administrator to register the 3-hour news brief task.

$taskName = "PromptAI_NewsAgent_3hr"
$batPath   = "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_news_brief.bat"

# Remove old task silently if it exists
schtasks /delete /tn $taskName /f 2>$null

# Create task: run every 3 hours, starting now, for 365 days
$startTime = (Get-Date).ToString("HH:mm")
$result = schtasks /create `
    /tn $taskName `
    /tr "`"$batPath`"" `
    /sc hourly `
    /mo 3 `
    /st $startTime `
    /du 8760:00 `
    /rl HIGHEST `
    /f

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nSuccess! Task '$taskName' will run every 3 hours starting at $startTime." -ForegroundColor Green
    schtasks /query /tn $taskName /fo LIST | Select-String "Next Run Time"
} else {
    Write-Host "`nFailed to register task. Make sure you ran this as Administrator." -ForegroundColor Red
}
