# Start the staged frozen service on its own port and data folder and check it
# answers and serves the HUD. Runs on Windows, macOS and Linux runners.
$ErrorActionPreference = "Stop"
$name = if ($IsWindows) { "burnrate-service.exe" } else { "burnrate-service" }
$exe = Join-Path "src-tauri/resources/burnrate-service" $name
if (-not (Test-Path $exe)) { throw "No staged service at $exe" }
$root = Join-Path ([System.IO.Path]::GetTempPath()) "burnrate-smoke-service"
New-Item -ItemType Directory -Force $root | Out-Null
$env:BURNRATE_DATA_ROOT = $root
$env:BURNRATE_DESKTOP_NOTIFICATIONS = "0"
$service = Start-Process -FilePath $exe -ArgumentList "--port", "17398" -PassThru
try {
    $up = $false
    for ($i = 0; $i -lt 120; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:17398/healthz" | Out-Null; $up = $true; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $up) {
        Get-ChildItem (Join-Path $root "logs") -ErrorAction SilentlyContinue | ForEach-Object { Get-Content $_.FullName -Tail 60 }
        throw "The frozen service never answered"
    }
    $hud = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:17398/hud"
    if ($hud.Content -notmatch "Sidecar HUD") { throw "The HUD page did not load" }
    $usage = Invoke-RestMethod "http://127.0.0.1:17398/api/spend/hud/usage" -TimeoutSec 60
    Write-Host "Frozen service answered after $([math]::Round($i / 2, 1)) s; HUD usage generated at $($usage.generatedAt)"
}
finally {
    Stop-Process -Id $service.Id -Force -ErrorAction SilentlyContinue
}
