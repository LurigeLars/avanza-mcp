param(
    [string]$TaskName = "AvanzaMcpHttpServer"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$launcher = Join-Path $PSScriptRoot "run-public-http-hidden.py"
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$fastmcp = Join-Path $repoRoot ".venv\Scripts\fastmcp.exe"

if (-not (Test-Path $pythonw) -or -not (Test-Path $fastmcp)) {
    throw "Project virtualenv is missing. Run: uv sync --locked"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $repoRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logonTrigger, $watchdogTrigger) -Principal $principal -Settings $settings -Description "Runs the loopback-only Avanza FastMCP HTTP server without a visible terminal window." -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Task '$TaskName' registered but not started because port 8767 is already in use by PID $($listener.OwningProcess)."
    Write-Host "Stop the existing manual FastMCP process, then run:"
    Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started. State: $($task.State)"
Write-Host "Log: $env:LOCALAPPDATA\avanza-mcp\public-http.log"
