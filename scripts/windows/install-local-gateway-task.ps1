param(
    [string]$TaskName = "AvanzaMcpLocalGateway"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$launcher = Join-Path $PSScriptRoot "run-local-gateway-hidden.py"
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$node = Get-Command node.exe -ErrorAction SilentlyContinue

if (-not (Test-Path $pythonw)) {
    throw "Project virtualenv is missing. Run: uv sync --locked"
}
if (-not $node) {
    throw "node.exe was not found on PATH"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$launcherArg = '"{0}"' -f $launcher
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $launcherArg -WorkingDirectory $repoRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logonTrigger, $watchdogTrigger) -Principal $principal -Settings $settings -Description "Runs the loopback-only model-optimized Avanza MCP gateway on 127.0.0.1:8769 without a visible terminal window." -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort 8769 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Task '$TaskName' registered but not started because port 8769 is already in use by PID $($listener.OwningProcess)."
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started. State: $($task.State)"
Write-Host "Optimized MCP URL: http://127.0.0.1:8769/mcp"
Write-Host "Log: $env:LOCALAPPDATA\avanza-mcp\local-gateway.log"
