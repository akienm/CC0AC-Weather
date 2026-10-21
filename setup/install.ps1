# CC0AC-Weather on Windows 10 or 11. In PowerShell (not as administrator):
#
#   irm https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.ps1 | iex
#
# or, from a copy of the repository:   powershell -ExecutionPolicy Bypass -File setup\install.ps1 -Source .
#
# It installs Python for you if it is missing (winget), then hands over to the boot
# loader (updates\bootloader.py, UPDATES.md), which downloads the newest release,
# adds a task that starts the station when you log on, and keeps it patched. Safe
# to run again: an existing config.ini is never touched.
#
# Everything goes in %LOCALAPPDATA%\CC0AC-Weather; settings in
# %LOCALAPPDATA%\CC0AC-Weather\station\.cc0ac-weather\config.ini.
# Windows can't take the AcuRite hub's cable; it uses the radio dongle, console
# uploads, or the internet's weather.
param(
    [string]$Source = "",   # install the program from this folder instead of downloading a release
    [string]$Tag = ""       # install this release (default: the newest)
)
$ErrorActionPreference = "Stop"
$Raw = "https://raw.githubusercontent.com/akienm/CC0AC-Weather/main"
$Root = Join-Path $env:LOCALAPPDATA "CC0AC-Weather"
$StationHome = Join-Path $Root "station"
$Config = Join-Path $StationHome ".cc0ac-weather\config.ini"

function Find-Python {
    # The "python" in a fresh Windows only opens the Microsoft Store, so each one is asked to run.
    $found = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $found += (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
    $found += (Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    $found += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($p in $found) {
        if (-not $p) { continue }
        $out = & $p -c "import sys, ssl, sqlite3; print(sys.version_info >= (3, 9))" 2>$null
        if ($out -eq "True") { return $p }
    }
    return $null
}

Write-Host "Installing CC0AC-Weather for: windows"
$python = Find-Python
if (-not $python) {
    Write-Host "`n== Python (from winget)"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3 is needed. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
    }
    winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    $python = Find-Python
    if (-not $python) { throw "Python was installed but can't be found yet. Open a new PowerShell window and run this again." }
}
Write-Host "Python: $python"

Write-Host "`n== The boot loader"
$work = Join-Path ([IO.Path]::GetTempPath()) ("cc0ac-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $bootloader = Join-Path $work "bootloader.py"
    if ($Source) {
        $Source = (Resolve-Path $Source).Path
        Copy-Item (Join-Path $Source "updates\bootloader.py") $bootloader
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$Raw/updates/bootloader.py" -OutFile $bootloader
    }
    $blArgs = @($bootloader, "install", "--platform", "windows", "--root", $Root, "--config", $Config,
              "--home", $StationHome, "--python", $python)
    if ($Source) { $blArgs += @("--from", $Source) }
    if ($Tag) { $blArgs += @("--tag", $Tag) }
    & $python @blArgs
    if ($LASTEXITCODE -ne 0) { throw "The boot loader stopped (exit $LASTEXITCODE); its log is $Root\updates.log" }
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

Write-Host @"

All set. The station starts now and each time you log on. Open the setup page:

    http://localhost:12345/setup

If Windows asks whether Python may use the network, say yes for private networks,
so phones and other computers in the house can see the dashboard.

Status:   & "$python" "$Root\bootloader.py" status
How updates work: https://github.com/akienm/CC0AC-Weather/blob/main/UPDATES.md
"@
