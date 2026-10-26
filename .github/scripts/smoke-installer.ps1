# Install the built NSIS installer on a clean runner, start the app, and check
# that its bundled service answers; then uninstall. No provider keys.
param(
    [string]$Installer = (Get-ChildItem "src-tauri/target/release/bundle/nsis/*-setup.exe" | Select-Object -First 1).FullName
)
$ErrorActionPreference = "Stop"
if (-not $Installer) { throw "No NSIS installer found" }
$installDir = Join-Path $env:LOCALAPPDATA "BURNRATE"

Write-Host "Installing $Installer"
Start-Process -FilePath $Installer -ArgumentList "/S" -Wait
foreach ($file in "burnrate-desktop.exe", "burnrate-service\burnrate-service.exe", "uninstall.exe") {
    if (-not (Test-Path (Join-Path $installDir $file))) { throw "Missing after install: $file" }
}

$env:BURNRATE_UPDATES = "0"
$app = Start-Process -FilePath (Join-Path $installDir "burnrate-desktop.exe") -PassThru
$up = $false
for ($i = 0; $i -lt 180; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:17331/healthz" | Out-Null; $up = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $up) {
    Get-ChildItem (Join-Path $installDir "logs") -ErrorAction SilentlyContinue | ForEach-Object { Get-Content $_.FullName -Tail 40 }
    throw "The bundled service never answered"
}
Write-Host "Service answered after $([math]::Round($i / 2, 1)) s"
$hud = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:17331/hud"
if ($hud.StatusCode -ne 200 -or $hud.Content -notmatch "Sidecar HUD") { throw "The HUD page did not load" }
$state = Invoke-RestMethod "http://127.0.0.1:17331/api/setup/state"
Write-Host "Setup state answered (timezone $($state.timezone))"

$services = @(Get-CimInstance Win32_Process -Filter "Name='burnrate-service.exe'")
if ($services.Count -lt 1) { throw "No burnrate-service.exe process" }
$run = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (-not ($run.PSObject.Properties | Where-Object { "$($_.Value)" -like "*burnrate-desktop*" })) {
    throw "An installed app should start at sign-in by default"
}

# A crash of the app must not leave the service behind (kill-on-close job).
Stop-Process -Id $app.Id -Force
Start-Sleep -Seconds 4
$left = @(Get-CimInstance Win32_Process -Filter "Name='burnrate-service.exe'")
if ($left.Count) { throw "The service outlived the app ($($left.Count) process(es))" }

Write-Host "Uninstalling"
# In place: NSIS otherwise copies the uninstaller to %TEMP% and returns at once
Start-Process -FilePath (Join-Path $installDir "uninstall.exe") -ArgumentList "/S", "_?=$installDir" -Wait
Start-Sleep -Seconds 3
if (Test-Path (Join-Path $installDir "burnrate-desktop.exe")) { throw "Uninstall left the app behind" }
if (-not (Test-Path (Join-Path $installDir "spend.db"))) { throw "Uninstall removed usage data it should keep" }
Write-Host "Installer smoke test passed"
