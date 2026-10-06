<#
.SYNOPSIS
    Installs TodoTracker for the current user.

.DESCRIPTION
    - Startup\TodoTracker.lnk  runs "pythonw.exe <folder>\todo.pyw --background" at login
      (no window; press Ctrl+Alt+T to bring it up).
    - Start menu\TodoTracker.lnk opens the window.
    - Starts the app.
    Nothing is copied: the app runs from this folder and keeps its data in .\data.

.PARAMETER OpenAtLogin
    Open the window at login too (instead of starting in the background).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -OpenAtLogin
#>
[CmdletBinding()]
param(
    [switch]$OpenAtLogin
)

$ErrorActionPreference = 'Stop'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $AppDir 'todo.pyw'
if (-not (Test-Path -LiteralPath $Script)) {
    throw "todo.pyw was not found next to install.ps1 ($AppDir)."
}

function Test-Python([string]$Pythonw) {
    # A usable pythonw.exe has a python.exe next to it that reports 3.8 or newer.
    if (-not $Pythonw -or -not (Test-Path -LiteralPath $Pythonw)) { return $false }
    $python = Join-Path (Split-Path -Parent $Pythonw) 'python.exe'
    if (-not (Test-Path -LiteralPath $python)) { return $true }
    $version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $version) { return $false }
    $parts = "$version".Trim().Split('.')
    return ([int]$parts[0] -gt 3) -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 8)
}

function Find-Pythonw {
    $candidates = New-Object System.Collections.Generic.List[string]
    # 1. The py launcher knows the installed Pythons best.
    $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) {
            $candidates.Add((Join-Path (Split-Path -Parent "$exe".Trim()) 'pythonw.exe'))
        }
    }
    # 2. pythonw.exe / python.exe on PATH (the Microsoft Store aliases last).
    foreach ($name in 'pythonw.exe', 'python.exe') {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            $candidates.Add((Join-Path (Split-Path -Parent $cmd.Source) 'pythonw.exe'))
        }
    }
    $ordered = @($candidates | Where-Object { $_ -notlike '*\WindowsApps\*' }) +
               @($candidates | Where-Object { $_ -like '*\WindowsApps\*' })
    foreach ($c in $ordered) {
        if (Test-Python $c) { return $c }
    }
    throw 'Python 3 was not found. Install Python 3.12 or newer from python.org (tick "Add python.exe to PATH") and run install.ps1 again.'
}

$Pythonw = Find-Pythonw
Write-Host "Python:  $Pythonw"
Write-Host "App:     $AppDir"

$Shell = New-Object -ComObject WScript.Shell

function New-AppShortcut([string]$Path, [string]$Arguments, [string]$Description) {
    $folder = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath = $Pythonw
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $AppDir
    $link.Description = $Description
    $link.Save()
    Write-Host "Created: $Path"
}

$Quoted = '"' + $Script + '"'
$StartupArgs = if ($OpenAtLogin) { $Quoted } else { "$Quoted --background" }
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'TodoTracker.lnk'
$MenuLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'TodoTracker.lnk'

New-AppShortcut $StartupLink $StartupArgs 'TodoTracker (starts at login; Ctrl+Alt+T shows it)'
New-AppShortcut $MenuLink $Quoted 'TodoTracker'

# Start it now. A running older copy from this folder is replaced automatically.
Start-Process -FilePath $Pythonw -ArgumentList $Quoted -WorkingDirectory $AppDir
Write-Host ''
Write-Host 'TodoTracker is installed. It starts at login; press Ctrl+Alt+T to bring it up.'
Write-Host "Your data stays in $(Join-Path $AppDir 'data')."
