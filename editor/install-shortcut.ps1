<#
.SYNOPSIS
    Puts "Pocketful Editor" in the Start menu and on the desktop.

.DESCRIPTION
    The shortcut runs editor\server.py under pythonw with --app: no console window, the
    editor opens in a window of its own (Edge's app mode, no tabs or address bar), and
    the server exits when that window is closed.

    A shortcut rather than a checked-in .lnk because a shortcut holds absolute paths --
    to this clone and to this machine's Python -- so it has to be made where it is used.
    Run it again after moving the repository or reinstalling Python.

.PARAMETER NoDesktop
    Start menu only.

.PARAMETER Remove
    Delete the shortcuts this script made.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File editor\install-shortcut.ps1
#>
param(
    [switch]$NoDesktop,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$editor = $PSScriptRoot
$server = Join-Path $editor 'server.py'
$icon = Join-Path $editor 'icon.ico'

$places = @([Environment]::GetFolderPath('Programs'))
if (-not $NoDesktop) { $places += [Environment]::GetFolderPath('Desktop') }

if ($Remove) {
    foreach ($dir in $places) {
        $path = Join-Path $dir 'Pocketful Editor.lnk'
        if (Test-Path $path) {
            Remove-Item $path
            Write-Host "Removed $path"
        }
    }
    return
}

# pythonw is python without a console. It sits beside python.exe in every python.org
# install, so failing to find it on PATH is worth one more look before giving up.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) {
        $candidate = Join-Path (Split-Path $python) 'pythonw.exe'
        if (Test-Path $candidate) { $pythonw = $candidate }
    }
}
if (-not $pythonw) {
    throw 'Could not find pythonw.exe. Install Python from python.org, then run this again.'
}

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $places) {
    $path = Join-Path $dir 'Pocketful Editor.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$server`" --app"
    $link.WorkingDirectory = Split-Path $editor
    $link.IconLocation = "$icon,0"
    $link.Description = 'Build, review and publish the Pocketful catalog'
    $link.Save()
    Write-Host "Created $path"
}
