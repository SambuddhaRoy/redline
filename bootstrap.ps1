<#
Peregrine bootstrap. Sets up and runs the Peregrine backend inside its own WSL distro.
Every command prints JSON lines on stdout so the desktop shell can show progress.

  check      GPU, driver, WSL and backend state
  install    create the distro if needed, copy the backend in, run setup.sh. Safe to re-run.
  sync       copy the backend code into the distro (dev loop)
  start      run the backend. Blocks: the shell keeps this process alive for the backend's lifetime.
  stop       stop the backend and the distro
  uninstall  delete the distro and everything in it, models included. Needs -Yes.

Exit codes: 0 ok, 1 error, 2 needs administrator, 3 reboot required.
#>
param(
  [Parameter(Position = 0)][ValidateSet('check', 'install', 'sync', 'start', 'stop', 'uninstall')][string]$Command = 'check',
  # Not %LOCALAPPDATA%\Peregrine: that's where the app installer puts the app, and uninstalling it would take the distro along.
  [string]$Location = (Join-Path $env:LOCALAPPDATA 'PeregrineData'),
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'   # otherwise wsl.exe prints UTF-16
# Read wsl.exe's UTF-8 as UTF-8 (non-ASCII user paths) and emit our JSON lines as UTF-8 without a BOM.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
$Distro = 'Peregrine'
$Port = 47800
$ImageUrl = 'https://releases.ubuntu.com/24.04.5/ubuntu-24.04.5-wsl-amd64.wsl'
$ImageSha = 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'
$ImageSize = 388975696
$Backend = Join-Path $PSScriptRoot 'backend'

function Emit([hashtable]$o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress -Depth 6)) }
function Fail([string]$step, [string]$msg, [int]$code = 1) { Emit @{ step = $step; status = 'error'; message = $msg }; exit $code }

function Get-Gpu {
  if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $null }
  $line = & nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version,compute_cap --format=csv,noheader,nounits | Select-Object -First 1
  $f = $line -split ',\s*'
  @{ name = $f[0]; vram_mib = [int]$f[1]; used_mib = [int]$f[2]; driver = $f[3]
     compute_cap = [double]::Parse($f[4], [Globalization.CultureInfo]::InvariantCulture) }
}

function Get-WslVersion {
  try { $out = & wsl.exe --version 2>$null } catch { return $null }
  if ($LASTEXITCODE -ne 0) { return $null }
  $m = [regex]::Match(($out -join "`n"), 'WSL[^:]*:\s*([\d.]+)')
  if ($m.Success) { return $m.Groups[1].Value }
  return $null
}

function Test-Distro { @(& wsl.exe -l -q 2>$null | ForEach-Object { $_.Trim() }) -contains $Distro }

function Test-Backend {
  try { Invoke-RestMethod "http://127.0.0.1:$Port/manage/status" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Get-Problems($gpu, $wsl) {
  $p = @()
  if (-not $gpu) { $p += 'No NVIDIA GPU or driver found (nvidia-smi is missing).' }
  else {
    if ($gpu.compute_cap -lt 8.0) { $p += "$($gpu.name) is too old. TensorRT-LLM needs an RTX 30-series or newer." }
    if ([int]($gpu.driver -split '\.')[0] -lt 580) { $p += "Driver $($gpu.driver) is too old. Install 580 or newer (615+ recommended)." }
  }
  if (-not $wsl) { $p += 'WSL is not installed.' }
  return $p
}

function Sync-Backend {
  $src = (& wsl.exe -d $Distro -u root --exec wslpath -u ($Backend -replace '\\', '/')).Trim()
  & wsl.exe -d $Distro -u root --exec bash "$src/sync.sh" $src
  if ($LASTEXITCODE -ne 0) { Fail 'sync' "Copying the backend into the distro failed ($LASTEXITCODE)." }
  Emit @{ step = 'sync'; status = 'done' }
}

switch ($Command) {
  'check' {
    $gpu = Get-Gpu
    $wsl = Get-WslVersion
    $hasDistro = [bool]($wsl -and (Test-Distro))
    Emit @{ step = 'check'; gpu = $gpu; wsl = $wsl; distro = $hasDistro; backend = (Test-Backend)
            location = $Location; problems = @(Get-Problems $gpu $wsl) }
  }

  'install' {
    $gpu = Get-Gpu
    $wsl = Get-WslVersion
    $problems = @(Get-Problems $gpu $wsl | Where-Object { $_ -ne 'WSL is not installed.' })
    if ($problems.Count) { Fail 'check' ($problems -join ' ') }

    if (-not $wsl) {
      $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
      if (-not $admin) { Fail 'wsl' 'WSL is not installed. Run install again as administrator.' 2 }
      Emit @{ step = 'wsl'; status = 'start' }
      & wsl.exe --install --no-distribution | Out-Null
      Emit @{ step = 'wsl'; status = 'reboot_required' }
      exit 3
    }

    if (-not (Test-Distro)) {
      $cache = Join-Path $Location 'cache'
      New-Item -ItemType Directory -Force $cache | Out-Null
      $img = Join-Path $cache 'ubuntu-24.04.5-wsl-amd64.wsl'
      if ((Test-Path $img) -and (Get-Item $img).Length -ge $ImageSize -and (Get-FileHash $img -Algorithm SHA256).Hash -ne $ImageSha) {
        Remove-Item $img
      }
      if (-not (Test-Path $img) -or (Get-Item $img).Length -lt $ImageSize) {
        Emit @{ step = 'image'; status = 'start'; total = $ImageSize }
        $p = Start-Process curl.exe -ArgumentList '-fsSL', '-C', '-', '-o', "`"$img`"", $ImageUrl -NoNewWindow -PassThru
        $null = $p.Handle   # PowerShell 5.1 drops ExitCode unless the handle is opened before exit
        while (-not $p.HasExited) {
          Start-Sleep -Seconds 1
          if (Test-Path $img) { Emit @{ step = 'image'; status = 'progress'; bytes = (Get-Item $img).Length; total = $ImageSize } }
        }
        if ($p.ExitCode -ne 0) { Fail 'image' "Ubuntu image download failed (curl exit $($p.ExitCode))." }
      }
      if ((Get-FileHash $img -Algorithm SHA256).Hash -ne $ImageSha) { Remove-Item $img; Fail 'image' 'Ubuntu image checksum mismatch. Try again.' }
      Emit @{ step = 'image'; status = 'done' }

      Emit @{ step = 'distro'; status = 'start' }
      $out = & wsl.exe --install --from-file $img --name $Distro --location (Join-Path $Location 'distro') --no-launch
      if ($LASTEXITCODE -ne 0) { Fail 'distro' "wsl --install failed ($LASTEXITCODE): $out" }
      Emit @{ step = 'distro'; status = 'done' }
    }

    Sync-Backend
    & wsl.exe -d $Distro -u root --exec bash /opt/peregrine/app/setup.sh | ForEach-Object { [Console]::Out.WriteLine($_) }
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Emit @{ step = 'install'; status = 'done' }
  }

  'sync' { Sync-Backend }

  'start' {
    if (Test-Backend) { Emit @{ step = 'start'; status = 'running'; url = "http://127.0.0.1:$Port" }; exit 0 }
    Emit @{ step = 'start'; status = 'start'; url = "http://127.0.0.1:$Port" }
    & wsl.exe -d $Distro -u root --exec /opt/peregrine/app/run.sh
    exit $LASTEXITCODE
  }

  'stop' {
    & wsl.exe --terminate $Distro | Out-Null
    Emit @{ step = 'stop'; status = 'done' }
  }

  'uninstall' {
    if (-not $Yes) { Fail 'uninstall' 'This deletes the distro and every downloaded model. Pass -Yes to confirm.' }
    if (Test-Distro) { & wsl.exe --unregister $Distro | Out-Null }
    # Only what we created: -Location may be a folder that holds other files.
    foreach ($d in 'cache', 'distro') { $p = Join-Path $Location $d; if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
    if ((Test-Path $Location) -and -not (Get-ChildItem $Location -Force)) { Remove-Item $Location }
    Emit @{ step = 'uninstall'; status = 'done' }
  }
}
