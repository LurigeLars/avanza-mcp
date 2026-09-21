param(
    [Parameter(Mandatory = $true)]
    [string]$UvPath,
    [int]$Port = 8767
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDir = Join-Path $env:LOCALAPPDATA "avanza-mcp"
$logFile = Join-Path $logDir "public-http.log"
$oldLogFile = Join-Path $logDir "public-http.log.1"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

if (Test-Path $logFile) {
    $length = (Get-Item $logFile).Length
    if ($length -ge 5MB) {
        Move-Item $logFile $oldLogFile -Force
    }
}

Set-Location $repoRoot
& $UvPath run --locked fastmcp run src/avanza_mcp/__init__.py:mcp --transport http --host 127.0.0.1 --port $Port *>> $logFile
exit $LASTEXITCODE
