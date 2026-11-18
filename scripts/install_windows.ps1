# Installs WoW Invest for the current Windows user and puts an icon on the desktop.
# Run it via install.cmd. Re-running it updates the program; your data is kept.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$base = Join-Path $env:LOCALAPPDATA 'WowInvest'
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'

if (-not (Test-Path $uv)) {
    # a fixed uv release, checked against its published SHA-256 before anything from it runs.
    # To move to a newer uv, change both lines (the hash is the .zip.sha256 file beside the download).
    $uvVersion = '0.13.9'
    $uvSha256 = 'edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb'
    Write-Host "Installing uv $uvVersion (a Python manager) for your user..."
    $zip = Join-Path $env:TEMP "uv-$uvVersion.zip"
    Invoke-WebRequest -UseBasicParsing -OutFile $zip "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
    if ((Get-FileHash -Algorithm SHA256 $zip).Hash -ne $uvSha256) {
        Remove-Item -Force $zip
        throw 'The uv download does not match its expected checksum. Nothing was installed.'
    }
    Expand-Archive -Force $zip (Split-Path $uv)
    Remove-Item -Force $zip
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
# the new copy is made complete beside the working one, then swapped in: a failed copy leaves the old program intact
$src = Join-Path $base 'src'
$new = Join-Path $base 'src.new'
$old = Join-Path $base 'src.old'
foreach ($d in $new, $old) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
New-Item -ItemType Directory -Force $new | Out-Null
Copy-Item -Recurse (Join-Path $repo 'wow_invest') $new
Get-ChildItem $new -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
if (Test-Path $src) { Rename-Item $src $old }
Rename-Item $new $src
if (Test-Path $old) { Remove-Item -Recurse -Force $old }
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
