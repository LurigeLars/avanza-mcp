param(
    [string]$TaskName = "Avanza MCP Public HTTP",
    [int]$Port = 8767
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runner = Join-Path $PSScriptRoot "run-public-http.ps1"
$uv = Get-Command uv.exe -ErrorAction Stop
$shell = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if (-not $shell) { $shell = Get-Command powershell.exe -ErrorAction Stop }
$userId = "$env:USERDOMAIN\$env:USERNAME"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -UvPath `"$($uv.Source)`" -Port $Port"
$action = New-ScheduledTaskAction -Execute $shell.Source -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Runs the loopback-only Avanza FastMCP HTTP server on 127.0.0.1:$Port without a visible terminal window." -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Task '$TaskName' registered but not started because port $Port is already in use by PID $($listener.OwningProcess)."
    Write-Host "Stop the existing manual FastMCP process, then run:"
    Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started. State: $($task.State)"
Write-Host "Log: $env:LOCALAPPDATA\avanza-mcp\public-http.log"
