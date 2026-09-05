@echo off
REM Registers the Prompt AI News schedule:
REM   - Unified briefing: daily at 6:00 AM, 12:00 PM, 6:00 PM
REM   - Weekly cost report: Sundays at 8:00 PM
REM Removes the old 3-hour task.
REM Run as Administrator.

schtasks /delete /tn "PromptAI_NewsAgent_3hr" /f 2>nul
schtasks /create /tn "PromptAI_Briefing_Morning"  /tr "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_briefing.bat" /sc daily /st 06:00 /f
schtasks /create /tn "PromptAI_Briefing_Midday"   /tr "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_briefing.bat" /sc daily /st 12:00 /f
schtasks /create /tn "PromptAI_Briefing_Evening"  /tr "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_briefing.bat" /sc daily /st 18:00 /f
schtasks /create /tn "PromptAI_WeeklyReport"      /tr "C:\Users\mrcar\OneDrive\Desktop\Test_Folder\run_weekly_report.bat" /sc weekly /d SUN /st 20:00 /f

echo.
echo Registered tasks:
schtasks /query /fo list | findstr /i "PromptAI"
pause
