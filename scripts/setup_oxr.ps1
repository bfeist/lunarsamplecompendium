param([switch]$DownloadOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$revision = 'c1a1dcb312ee9f691404f127785510453c0d9350'
$modelRevision = 'c708407f9d2b4df658f2543d9132b62bb7221df4'

function Check-Exit([string]$step) {
    if ($LASTEXITCODE -ne 0) { throw "$step failed (exit $LASTEXITCODE)" }
}

if (!(Test-Path '.local/oxr/.git')) {
    git clone https://github.com/SoMarkAI/OXR.git .local/oxr
    Check-Exit 'Clone OXR'
}
$current = git -C .local/oxr rev-parse HEAD
Check-Exit 'Read OXR revision'
if ($current -ne $revision) {
    $changes = git -C .local/oxr status --porcelain
    Check-Exit 'Check OXR checkout'
    if ($changes) { throw 'The OXR checkout has local changes; preserve them before changing revisions.' }
    git -C .local/oxr fetch origin $revision
    Check-Exit 'Fetch OXR revision'
    git -C .local/oxr checkout --detach $revision
    Check-Exit 'Pin OXR revision'
}
uv run --with 'huggingface-hub==2.4.0' python -c "from huggingface_hub import snapshot_download; snapshot_download('SoMarkAI/OXR-1.0', revision='$modelRevision', local_dir='.local/models/OXR-1.0')"
Check-Exit 'Download OXR weights'
if (!$DownloadOnly) {
    docker compose -f compose.oxr.yaml up -d --build --wait --wait-timeout 960
    Check-Exit 'Start OXR'
    Write-Host 'OXR ready: http://localhost:8010/demo'
}
