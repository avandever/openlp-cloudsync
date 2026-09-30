<#
.SYNOPSIS
    Installs (or upgrades) the OpenLP Cloud Sync plugin.

.DESCRIPTION
    Copies the plugin files from the folder next to this script into
    OpenLP's community plugin directory:

        %APPDATA%\openlp\data\contrib\plugins\cloudsync\

    An existing install is moved to a timestamped backup first; your
    sign-in token and sync state live elsewhere and are never touched.

    Double-click Install.bat, or run:
        powershell -ExecutionPolicy Bypass -File Install-CloudSync.ps1

.PARAMETER Uninstall
    Remove the plugin instead of installing it.

.PARAMETER Force
    Skip the "close OpenLP first" prompt.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$PluginName = 'cloudsync'
$PayloadDir = Join-Path $PSScriptRoot $PluginName
$DataDir    = Join-Path $env:APPDATA 'openlp\data'
$TargetDir  = Join-Path $DataDir "contrib\plugins\$PluginName"

function Read-Version {
    $versionFile = Join-Path $PayloadDir 'version.txt'
    if (Test-Path $versionFile) { return (Get-Content $versionFile -Raw).Trim() }
    return 'unknown'
}

if ($Uninstall) {
    if (Test-Path $TargetDir) {
        $backup = "$TargetDir.uninstalled-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item $TargetDir $backup
        Write-Host "Removed $TargetDir"
        Write-Host "(kept a copy at $backup)"
    } else {
        Write-Host "Nothing to remove: $TargetDir does not exist."
    }
    return
}

# --- sanity checks ---------------------------------------------------------
if (-not (Test-Path (Join-Path $PayloadDir 'cloudsyncplugin.py'))) {
    Write-Error "Installer files are incomplete: could not find `"$PayloadDir\cloudsyncplugin.py`"."
}
if (-not (Test-Path $DataDir)) {
    Write-Error ("OpenLP data folder not found at `"$DataDir`". " +
                 "Install OpenLP and run it once first, then re-run this installer.")
}

$openlp = Get-Process -Name 'openlp' -ErrorAction SilentlyContinue
if ($openlp -and -not $Force) {
    Write-Warning 'OpenLP is running. Close it before installing, then press Enter to continue.'
    Read-Host 'Press Enter when OpenLP is closed (or Ctrl+C to cancel)' | Out-Null
}

# --- backup existing install ----------------------------------------------
if (Test-Path $TargetDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$TargetDir.backup-$stamp"
    Move-Item $TargetDir $backup
    Write-Host "Backed up previous install to $backup"
}

# --- install ---------------------------------------------------------------
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
Copy-Item (Join-Path $PayloadDir '*') $TargetDir -Recurse -Force

$check = Join-Path $TargetDir 'cloudsyncplugin.py'
if (-not (Test-Path $check)) {
    Write-Error "Install failed: `"$check`" is missing after copy."
}

$version = Read-Version
@"
OpenLP Cloud Sync plugin v$version
Installed $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') by Install-CloudSync.ps1
"@ | Set-Content (Join-Path $TargetDir 'installed-by.txt')

Write-Host ''
Write-Host "Installed Cloud Sync v$version to $TargetDir" -ForegroundColor Green
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1. Start OpenLP.'
Write-Host '  2. If the plugin does not appear, enable it under Settings > Plugins.'
Write-Host '  3. Open the Cloud Sync settings and sign in with Google if prompted.'
