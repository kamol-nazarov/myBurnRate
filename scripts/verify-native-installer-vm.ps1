param([Parameter(Mandatory = $true)][string]$Installer)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $Installer).Path
$settings = 'HKCU:\Software\BURNRATE Native'
$registration = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BURNRATENative'
$dataRoot = Join-Path $env:LOCALAPPDATA 'BURNRATE'
if ((Test-Path -LiteralPath $settings) -or (Test-Path -LiteralPath $registration)) {
    throw 'The disposable runner already has a myBurnRate installation registered.'
}

function Invoke-InstallerProcess {
    param([string]$Image, [string]$Arguments)
    $process = Start-Process -FilePath $Image -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(90000)) {
        $process.Kill()
        throw "Timed out waiting for $Image"
    }
    $process.ExitCode
}

function Assert-Content {
    param([string]$Path, [string]$Expected)
    if ((Get-Content -Raw -LiteralPath $Path) -cne $Expected) {
        throw "Unexpected content at $Path"
    }
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ('burnrate-installer-' + [guid]::NewGuid().ToString('N'))
$foreign = Join-Path $fixture 'foreign'
$custom = Join-Path $fixture 'custom'
$foreignRuntime = Join-Path $foreign '_internal'
[void][IO.Directory]::CreateDirectory($foreignRuntime)
[void][IO.Directory]::CreateDirectory($dataRoot)
$foreignFile = Join-Path $foreignRuntime 'sentinel.txt'
$dataFile = Join-Path $dataRoot ((Split-Path -Leaf $fixture) + '.txt')
[IO.File]::WriteAllText($foreignFile, 'foreign runtime')
[IO.File]::WriteAllText($dataFile, 'usage-history sentinel')

if ((Invoke-InstallerProcess $exe "/S /D=$foreign") -ne 1) {
    throw 'The installer accepted a foreign nonempty directory.'
}
Assert-Content $foreignFile 'foreign runtime'
if (Test-Path -LiteralPath $settings) {
    throw 'The rejected installation changed the installation registration.'
}

if ((Invoke-InstallerProcess $exe "/S /D=$custom") -ne 0) {
    throw 'Fresh custom installation failed.'
}
$installPath = Get-ItemPropertyValue -LiteralPath $settings -Name InstallDir
if ($installPath -ine $custom) {
    throw "Expected installed path $custom, got $installPath"
}
$appExe = Join-Path $custom 'BURNRATE.exe'
$uninstaller = Join-Path $custom 'uninstall.exe'
$marker = Join-Path $custom 'BURNRATE.installed'
$markerData = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $appExe) -or $markerData.installer -cne 'nsis') {
    throw 'The fresh installation did not create its executable and ownership marker.'
}

# Exercise the updater's actual silent entry point. All app configuration is
# confined to this disposable user, with a fresh loopback port and data root.
$env:BURNRATE_DATA_ROOT = Join-Path $fixture 'runtime'
$env:SPEND_DATABASE_PATH = Join-Path $env:BURNRATE_DATA_ROOT 'spend.db'
$env:BURNRATE_CENTRAL_SERVER = '1'
$env:BURNRATE_USAGE_ONLY = '1'
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $env:BURNRATE_PORT = [string]$listener.LocalEndpoint.Port
} finally {
    $listener.Stop()
}
$staleRuntime = Join-Path $custom '_internal\obsolete-runtime.txt'
$preservedRoot = Join-Path $custom 'preserve.txt'
[IO.File]::WriteAllText($staleRuntime, 'remove during owned runtime upgrade')
[IO.File]::WriteAllText($preservedRoot, 'preserve unrelated root file')
if ((Invoke-InstallerProcess $exe "/S /UPDATE /D=$custom") -ne 0) {
    throw 'Silent upgrade of the owned custom installation failed.'
}
if (Test-Path -LiteralPath $staleRuntime) { throw 'The stale runtime was left installed.' }
Assert-Content $preservedRoot 'preserve unrelated root file'
Assert-Content $dataFile 'usage-history sentinel'

$displaced = Join-Path $foreign 'uninstall.exe'
Copy-Item -LiteralPath $uninstaller -Destination $displaced
if ((Invoke-InstallerProcess $displaced "/S _?=$foreign") -ne 1) {
    throw 'The displaced uninstaller did not fail with the guard error.'
}
Assert-Content $foreignFile 'foreign runtime'
if (-not (Test-Path -LiteralPath $registration)) {
    throw 'The displaced uninstaller removed the real fixture registration.'
}

if ((Invoke-InstallerProcess $uninstaller '/S') -ne 0) {
    throw 'The owned uninstaller failed.'
}
$deadline = [DateTime]::UtcNow.AddSeconds(45)
while (((Test-Path -LiteralPath $settings) -or (Test-Path -LiteralPath $appExe)) -and
       [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 250
}
foreach ($path in @($settings, $registration, $appExe, $marker, (Join-Path $custom '_internal'))) {
    if (Test-Path -LiteralPath $path) { throw "Uninstaller left an owned item at $path" }
}
Assert-Content $preservedRoot 'preserve unrelated root file'
Assert-Content $dataFile 'usage-history sentinel'
Assert-Content $foreignFile 'foreign runtime'
Write-Output "NSIS lifecycle passed in $fixture"
