<#
Builds a signed Redline installer and publishes it as a GitHub release that installed copies update from.

  .\release.ps1 -Notes "What changed"

Bump "version" in app/src-tauri/tauri.conf.json first. Needs the signing key at %USERPROFILE%\.tauri\redline.key
(back it up: without it, no installed copy will accept another update) and the gh CLI signed in.
#>
param([Parameter(Mandatory)][string]$Notes)
# 'Continue', with exit codes checked by hand: PowerShell 5.1 under 'Stop' aborts on any stderr text from
# native tools, and npm/tauri/gh all print progress there.
$ErrorActionPreference = 'Continue'
function Check([string]$what) { if ($LASTEXITCODE) { Write-Error "$what failed ($LASTEXITCODE)"; exit 1 } }

$repo = 'SambuddhaRoy/redline'
$conf = Get-Content (Join-Path $PSScriptRoot 'app\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$v = $conf.version
& gh release view "v$v" --repo $repo *> $null
if ($LASTEXITCODE -eq 0) { Write-Error "Release v$v already exists. Bump the version in tauri.conf.json first."; exit 1 }

$env:TAURI_SIGNING_PRIVATE_KEY = Get-Content (Join-Path $env:USERPROFILE '.tauri\redline.key') -Raw
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ''
Push-Location (Join-Path $PSScriptRoot 'app')
& npm run tauri build 2>&1 | Select-Object -Last 4
$code = $LASTEXITCODE
Pop-Location
$LASTEXITCODE = $code; Check 'Build'

$dir = Join-Path $PSScriptRoot 'app\src-tauri\target\release\bundle\nsis'
$exe = Join-Path $dir "Redline_${v}_x64-setup.exe"
if (-not (Test-Path "$exe.sig")) { Write-Error "No signature next to $exe"; exit 1 }
$latest = [ordered]@{
  version   = $v
  notes     = $Notes
  pub_date  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  platforms = @{ 'windows-x86_64' = @{
    signature = (Get-Content "$exe.sig" -Raw).Trim()
    url       = "https://github.com/$repo/releases/download/v$v/Redline_${v}_x64-setup.exe" } }
} | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $dir 'latest.json'), $latest) # UTF-8 without a BOM; the updater's JSON parser rejects one

& gh release create "v$v" $exe "$exe.sig" (Join-Path $dir 'latest.json') --repo $repo --title "Redline $v" --notes $Notes
Check 'Publishing the release'
