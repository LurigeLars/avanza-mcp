param(
    [string]$TaskName = "AvanzaMcpInstrumentCatalogRefresh",
    [string]$At = "06:00"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
$launcher = Join-Path $PSScriptRoot "run-instrument-catalog-refresh-hidden.py"
$pythonw = Join-Path $repoRoot ".venv\\Scripts\\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    throw "Project virtualenv is missing. Run: uv sync --locked"
}

try {
    $parsedTime = [DateTime]::ParseExact(
        $At,
        "HH:mm",
        [System.Globalization.CultureInfo]::InvariantCulture
    )
} catch {
    throw "At must use 24-hour HH:mm format, for example 06:00."
}

$userId = "$env:USERDOMAIN\\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $repoRoot
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $parsedTime
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $dailyTrigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Refreshes the local Avanza certificate/warrant discovery catalog once per day." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered for daily refresh at $At and started once now."
Write-Host "Catalog: $env:LOCALAPPDATA\\avanza-mcp\\instrument-catalog.sqlite3"
Write-Host "Log: $env:LOCALAPPDATA\\avanza-mcp\\instrument-catalog-refresh.log"
