[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

function Stop-WithError([string]$Message) {
    Write-Error "[start] $Message"
    exit 1
}

function Wait-Http([string]$Url, [int]$Seconds = 180) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
        } catch {}
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    Stop-WithError "Timed out waiting for $Url"
}

function Get-DotEnvValue([string]$Name) {
    $line = Get-Content -LiteralPath '.env' | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return $line.Substring($line.IndexOf('=') + 1).Trim()
}

function Set-DotEnvValue([string]$Name, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath '.env')
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^$([regex]::Escape($Name))=") {
            $lines[$index] = "$Name=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines.Add("$Name=$Value") }
    [System.IO.File]::WriteAllLines((Join-Path $ProjectRoot '.env'), $lines, [System.Text.UTF8Encoding]::new($false))
}

foreach ($command in 'docker', 'node', 'npm') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        Stop-WithError "$command is not installed or not on PATH."
    }
}
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host '[start] Created .env from .env.example.'
}
New-Item -ItemType Directory -Force -Path '.run', '.state' | Out-Null

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-WithError 'Docker Desktop is not running. Start it, select Docker VMM in Settings > General, then retry.'
}

$nodeMajor = [int]((node --version).TrimStart('v').Split('.')[0])
if ($nodeMajor -lt 22) { Stop-WithError "Node.js 22 or newer is required; found $(node --version)." }

Write-Host '[start] Building the Grafana AI plugin...'
Push-Location 'grafana-ai-plugin'
try {
    if (-not (Test-Path -LiteralPath 'node_modules\.bin\webpack.cmd')) { npm.cmd ci --no-audit --no-fund }
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'npm install failed.' }
    npm.cmd run typecheck
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'Plugin type checking failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'Plugin build failed.' }
} finally { Pop-Location }

if (-not (Test-Path -LiteralPath 'grafana-ai-plugin\dist\module.js')) {
    Stop-WithError 'Plugin build did not produce dist\module.js.'
}

Write-Host '[start] Starting Prometheus, demo exporter and Grafana...'
docker compose up -d --build prometheus demo grafana
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Docker could not start the base services. With Docker VMM, add $ProjectRoot in Settings > Resources > File Sharing, apply the change, and retry."
}
Wait-Http 'http://127.0.0.1:3000/api/health'
Wait-Http 'http://127.0.0.1:9090/-/healthy'
Wait-Http 'http://127.0.0.1:8000/health'

# A fresh Grafana volume has no MCP service-account token. Bootstrap it through
# the local admin API and store the secret only in the git-ignored .env file.
$grafanaToken = Get-DotEnvValue 'GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN'
if ([string]::IsNullOrWhiteSpace($grafanaToken) -or $grafanaToken -like 'replace-with-*') {
    Write-Host '[start] Creating the local Grafana MCP service account...'
    $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('admin:admin'))
    $headers = @{ Authorization = "Basic $basic" }
    try {
        $account = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3000/api/serviceaccounts' `
            -Headers $headers -ContentType 'application/json' -Body '{"name":"local-grafana-mcp","role":"Editor"}'
        $accountId = $account.id
    } catch {
        $search = Invoke-RestMethod -Uri 'http://127.0.0.1:3000/api/serviceaccounts/search?query=local-grafana-mcp' -Headers $headers
        $existing = @($search.serviceAccounts | Where-Object name -eq 'local-grafana-mcp') | Select-Object -First 1
        if (-not $existing) { Stop-WithError 'Could not create the Grafana service account. The local admin/admin login may have changed.' }
        $accountId = $existing.id
    }
    $tokenReply = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:3000/api/serviceaccounts/$accountId/tokens" `
        -Headers $headers -ContentType 'application/json' -Body '{"name":"local-mcp-token","secondsToLive":0}'
    Set-DotEnvValue 'GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN' $tokenReply.key
}

$callerToken = Get-DotEnvValue 'MCP_GRAFANA_SERVER_TOKEN'
if ([string]::IsNullOrWhiteSpace($callerToken) -or $callerToken -like 'replace-with-*') {
    $random = [byte[]]::new(32)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($random) } finally { $generator.Dispose() }
    Set-DotEnvValue 'MCP_GRAFANA_SERVER_TOKEN' (([BitConverter]::ToString($random) -replace '-', '').ToLowerInvariant())
}

# Stop the obsolete host-side bridge from older versions before Docker binds
# port 8010. The containerized bridge now survives terminal/logoff events and
# restarts together with the rest of the stack.
if (Test-Path -LiteralPath '.run\ai-api.pid') {
    $oldPid = [int](Get-Content -LiteralPath '.run\ai-api.pid' -ErrorAction SilentlyContinue)
    $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    $legacyPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if ($oldProcess -and (Test-Path -LiteralPath $legacyPython) -and
        $oldProcess.Path -eq (Resolve-Path -LiteralPath $legacyPython).Path) {
        Stop-Process -Id $oldPid -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath '.run\ai-api.pid' -Force -ErrorAction SilentlyContinue
}

Write-Host '[start] Starting Grafana MCP and the persistent AI bridge...'
docker compose up -d --build mcp-grafana ai-bridge
if ($LASTEXITCODE -ne 0) { Stop-WithError 'Docker could not start Grafana MCP and AI bridge.' }
Wait-Http 'http://127.0.0.1:8002/healthz'
Wait-Http 'http://127.0.0.1:8010/health'

# Grafana reads plugin metadata on startup. Reload it after each local build.
docker compose restart grafana | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-WithError 'Docker could not restart Grafana after the plugin build.' }
Wait-Http 'http://127.0.0.1:3000/api/health'
$basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('admin:admin'))
try {
    $pluginSettings = Invoke-RestMethod -Uri 'http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings' `
        -Headers @{ Authorization = "Basic $basic" }
    if (-not $pluginSettings.enabled) {
        $enableBody = @{ enabled = $true; pinned = [bool]$pluginSettings.pinned; jsonData = $pluginSettings.jsonData } | ConvertTo-Json -Depth 20
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings' `
            -Headers @{ Authorization = "Basic $basic" } -ContentType 'application/json' `
            -Body $enableBody | Out-Null
    }
} catch { Stop-WithError 'Grafana is running, but the AI app could not be enabled. Check the admin/admin login.' }

$proxyDeadline = (Get-Date).AddSeconds(30)
do {
    try {
        $proxy = Invoke-WebRequest -UseBasicParsing `
            -Uri 'http://127.0.0.1:3000/api/plugin-proxy/mkhco-ai-dashboard-app/ai/health' `
            -Headers @{ Authorization = "Basic $basic" } -TimeoutSec 3
        if ($proxy.StatusCode -eq 200) { break }
    } catch {}
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $proxyDeadline)
if (-not $proxy -or $proxy.StatusCode -ne 200) { Stop-WithError 'Grafana plugin proxy did not become healthy.' }

Write-Host ''
Write-Host 'Grafana AI stack is running on Windows.' -ForegroundColor Green
Write-Host 'Grafana:    http://localhost:3000'
Write-Host 'AI plugin:  http://localhost:3000/a/mkhco-ai-dashboard-app/'
Write-Host 'Prometheus: http://localhost:9090'
Write-Host 'Metrics:    http://localhost:8000/metrics'
Write-Host 'MCP:        http://127.0.0.1:8002/mcp'
Write-Host 'AI logs:    docker compose logs ai-bridge'
