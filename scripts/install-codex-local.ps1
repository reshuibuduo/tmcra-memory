[CmdletBinding()]
param(
    [string]$RuntimeConfig = '',
    [string]$IntegrationConfig = '',
    [string]$IntegrationStateDir = '',
    [string]$CodexPath = '',
    [string]$NodePath = 'node',
    [string]$WorkflowPython = '',
    [switch]$SkipPluginInstall
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Plugin = Join-Path $Repo 'integrations\local-agent-hooks'
if (-not $RuntimeConfig) {
    $RuntimeConfig = Join-Path $Repo '.tmcra\config\runtime\local-runtime.json'
}
$RuntimeConfig = (Resolve-Path -LiteralPath $RuntimeConfig).Path

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$FilePath,
        [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

Invoke-Native $NodePath '--version'
if (-not $WorkflowPython) { $WorkflowPython = $env:TMCRA_LOCAL_PYTHON }
if (-not $WorkflowPython) { $WorkflowPython = Join-Path $Repo '.tmcra\venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $WorkflowPython -PathType Leaf)) { throw 'Run install-local.ps1 first, or pass -WorkflowPython with the installed runtime Python.' }
$configureArguments = @((Join-Path $Plugin 'scripts\configure.mjs'), '--runtime-config', $RuntimeConfig, '--python', $WorkflowPython)
if ($IntegrationConfig) { $configureArguments += @('--output', $IntegrationConfig) }
if ($IntegrationStateDir) { $configureArguments += @('--state-dir', $IntegrationStateDir) }
Invoke-Native $NodePath @configureArguments

if (-not $SkipPluginInstall) {
    if (-not $CodexPath) {
        $command = Get-Command codex -ErrorAction SilentlyContinue
        if ($command -and $command.Source -and $command.Source -notlike '*WindowsApps*') {
            $CodexPath = $command.Source
        } else {
            $candidate = Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Recurse -Filter codex.exe -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 1
            if ($candidate) { $CodexPath = $candidate.FullName }
        }
    }
    if (-not $CodexPath) { throw 'Codex CLI was not found. Install or update Codex first.' }
    & $CodexPath plugin marketplace --help *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'The installed Codex CLI does not support plugin marketplaces.'
    }
    Invoke-Native $CodexPath 'features' 'enable' 'hooks'
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & $CodexPath plugin marketplace remove tmcra-owner-local --json *> $null
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    Invoke-Native $CodexPath 'plugin' 'marketplace' 'add' $Repo '--json'
    Invoke-Native $CodexPath 'plugin' 'add' 'tmcra-local-memory@tmcra-owner-local' '--json'
}

Write-Host 'TMCRA owner-local hooks are configured.'
if (-not $SkipPluginInstall) {
    Write-Host 'Restart Codex, open /hooks, review the four TMCRA Local Memory hooks, and trust them.'
}
