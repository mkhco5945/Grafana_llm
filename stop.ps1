[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if (Test-Path -LiteralPath '.run\ai-api.pid') {
    $processId = [int](Get-Content -LiteralPath '.run\ai-api.pid' -ErrorAction SilentlyContinue)
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    $pythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if ($process -and (Test-Path -LiteralPath $pythonPath) -and $process.Path -eq (Resolve-Path -LiteralPath $pythonPath).Path) {
        Stop-Process -Id $processId -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath '.run\ai-api.pid' -Force
}
if (Get-Command docker -ErrorAction SilentlyContinue) { docker compose stop }
Write-Host 'Docker services and the Windows AI bridge stopped. Grafana, Prometheus and chat data were preserved.'
