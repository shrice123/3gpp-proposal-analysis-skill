[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

$ErrorActionPreference = 'Stop'
$collector = Join-Path $PSScriptRoot 'collect_3gpp_evidence.py'

if (-not (Test-Path -LiteralPath $collector -PathType Leaf)) {
    throw "Collector script not found: $collector"
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 $collector @CollectorArguments
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python) {
    & $python.Source $collector @CollectorArguments
    exit $LASTEXITCODE
}

$codexPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $codexPython -PathType Leaf) {
    & $codexPython $collector @CollectorArguments
    exit $LASTEXITCODE
}

throw 'Python 3 was not found. Install Python 3.10 or newer, then retry.'
