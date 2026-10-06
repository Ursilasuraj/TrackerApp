<#
.SYNOPSIS
    Removes the TodoTracker shortcuts and stops the running app. Keeps the data.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Continue'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path

foreach ($folder in 'Startup', 'Programs') {
    $link = Join-Path ([Environment]::GetFolderPath($folder)) 'TodoTracker.lnk'
    if (Test-Path -LiteralPath $link) {
        Remove-Item -LiteralPath $link -Force
        Write-Host "Removed: $link"
    }
}

# Ask the app to stop (bypassing any proxy; the app only answers on 127.0.0.1).
try {
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/shutdown")
    $request.Method = 'POST'
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.ContentType = 'application/json'
    $request.Headers.Add('X-Todo', '1')
    $body = [System.Text.Encoding]::UTF8.GetBytes('{}')
    $request.ContentLength = $body.Length
    $stream = $request.GetRequestStream()
    $stream.Write($body, 0, $body.Length)
    $stream.Close()
    $request.GetResponse().Close()
    Write-Host 'Asked TodoTracker to stop.'
    Start-Sleep -Seconds 2
} catch {
    Write-Host 'TodoTracker was not answering (probably not running).'
}

# Anything from this folder that is still running gets stopped.
$pattern = '*' + (Join-Path $AppDir 'todo.pyw') + '*'
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like $pattern } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped process $($_.ProcessId)."
    }

Write-Host ''
Write-Host "TodoTracker is uninstalled. Your data is still in $(Join-Path $AppDir 'data')."
