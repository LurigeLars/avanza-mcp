param(
    [string]$TaskName = "AvanzaMcpAuthenticatedHttpServer"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$launcher = Join-Path $PSScriptRoot "run-auth-http-hidden.py"
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    throw "Project virtualenv is missing. Run: uv sync --locked --all-extras"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $repoRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logonTrigger, $watchdogTrigger) -Principal $principal -Settings $settings -Description "Loopback-only authenticated Avanza FastMCP HTTP server." -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort 8768 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Task '$TaskName' registered but not started because port 8768 is already used by PID $($listener.OwningProcess)."
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' installed. State: $($task.State)"
Write-Host "Log: $env:LOCALAPPDATA\avanza-mcp\authenticated-http.log"
