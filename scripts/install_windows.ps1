# Installs WoW Invest for the current Windows user and puts an icon on the desktop.
# Run it via install.cmd. Re-running it updates the program; your data is kept.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$base = Join-Path $env:LOCALAPPDATA 'WowInvest'
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'

if (-not (Test-Path $uv)) {
    Write-Host 'Installing uv (a Python manager) for your user...'
    powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
    if (-not (Test-Path $uv)) { throw 'uv did not install. See https://docs.astral.sh/uv/' }
}

Write-Host 'Getting Python 3.12...'
& $uv python install 3.12 | Out-Null
$py = (& $uv python find 3.12).Trim()
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'  # runs without a console window

New-Item -ItemType Directory -Force $base | Out-Null
Write-Host 'Installing the window library (pywebview)...'
& $uv pip install --quiet --upgrade --python $py --target (Join-Path $base 'lib') pywebview
if ($LASTEXITCODE) { throw 'Installing pywebview failed.' }

Write-Host 'Copying WoW Invest...'
$src = Join-Path $base 'src'
if (Test-Path $src) { Remove-Item -Recurse -Force $src }
New-Item -ItemType Directory -Force $src | Out-Null
Copy-Item -Recurse (Join-Path $repo 'wow_invest') $src
Get-ChildItem $src -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Copy-Item -Force (Join-Path $repo 'wow_invest\static\favicon.ico') (Join-Path $base 'wow_invest.ico')
@'
import os, sys
base = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(base, "src"), os.path.join(base, "lib")]
from wow_invest.app import run
raise SystemExit(run())
'@ | Set-Content -Encoding ASCII (Join-Path $base 'launch.pyw')

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'WoW Invest.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + (Join-Path $base 'launch.pyw') + '"'
$s.WorkingDirectory = $base
$s.IconLocation = (Join-Path $base 'wow_invest.ico') + ',0'
$s.Description = 'WoW Invest'
$s.Save()
Write-Host "Done. Double-click 'WoW Invest' on your desktop. Data is kept in $base"
