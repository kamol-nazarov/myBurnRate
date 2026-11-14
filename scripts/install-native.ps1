#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoLaunch
)

# Per-user installer helper for the myBurnRate native Windows desktop app.
# Downloads native-latest.json from the official GitHub release, verifies the
# installer URL, byte size, and SHA-256 digest before executing anything, runs
# the per-user NSIS installer (/S) into %LOCALAPPDATA%\Programs\BURNRATE, and
# opens BURNRATE.exe.
#
# Usage:
#   irm https://raw.githubusercontent.com/kamol-nazarov/myBurnRate/prod/scripts/install-native.ps1 | iex

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = 'kamol-nazarov/myBurnRate'
$FeedUrl = "https://github.com/$Repo/releases/latest/download/native-latest.json"

Write-Host "Checking latest myBurnRate release from $Repo..." -ForegroundColor Cyan
$FeedRaw = Invoke-WebRequest -Uri $FeedUrl -UseBasicParsing -TimeoutSec 30
$Feed = $FeedRaw.Content | ConvertFrom-Json

$Version = [string]$Feed.version
if ($Version -notmatch '^[0-9A-Za-z.+-]+$') {
    throw "Unexpected version format in native-latest.json: '$Version'"
}

$Win = $Feed.platforms.'windows-x86_64'
if (-not $Win) {
    throw 'No windows-x86_64 entry found in native-latest.json.'
}

$ExpectedName = "myBurnRate_${Version}_x64-setup.exe"
$ExpectedUrl = "https://github.com/$Repo/releases/download/v$Version/$ExpectedName"
if ([string]$Win.url -ne $ExpectedUrl) {
    throw "Unexpected installer URL in feed: $($Win.url)"
}

$ExpectedSha = ([string]$Win.sha256).Trim().ToLowerInvariant()
$ExpectedSize = [int64]$Win.size
if ($ExpectedSha -notmatch '^[0-9a-f]{64}$' -or $ExpectedSize -le 0 -or $ExpectedSize -gt 314572800) {
    throw 'Invalid size or SHA-256 metadata in native-latest.json.'
}

$WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("myburnrate-install-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$InstallerPath = Join-Path $WorkDir $ExpectedName

try {
    Write-Host "Downloading $ExpectedName ($([Math]::Round($ExpectedSize / 1MB, 1)) MB)..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $ExpectedUrl -OutFile $InstallerPath -UseBasicParsing -TimeoutSec 180

    $ActualSize = (Get-Item -LiteralPath $InstallerPath).Length
    if ($ActualSize -ne $ExpectedSize) {
        Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
        throw "Downloaded size ($ActualSize bytes) did not match expected size ($ExpectedSize bytes)."
    }

    $ActualSha = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha -ne $ExpectedSha) {
        Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
        throw "SHA-256 mismatch (got $ActualSha, expected $ExpectedSha)."
    }

    Write-Host "SHA-256 verified ($($ActualSha.Substring(0, 12))...). Installing per-user..." -ForegroundColor Green
    $sigUrl = $ExpectedUrl + '.sig'
    $SigPath = Join-Path $WorkDir ($ExpectedName + '.sig')
    Invoke-WebRequest -Uri $sigUrl -OutFile $SigPath -UseBasicParsing -TimeoutSec 30

    $VerifierPath = Join-Path $PSScriptRoot 'minisign-verify.ps1'
    if (-not (Test-Path -LiteralPath $VerifierPath)) {
        $VerifierPath = Join-Path $WorkDir 'minisign-verify.ps1'
        $VerifierUrl = "https://raw.githubusercontent.com/$Repo/prod/scripts/minisign-verify.ps1"
        Invoke-WebRequest -Uri $VerifierUrl -OutFile $VerifierPath -UseBasicParsing -TimeoutSec 30
    }
    . $VerifierPath
    Test-MinisignFile -Path $InstallerPath -SignaturePath $SigPath -ExpectedFile $ExpectedName
    Write-Host 'Minisign signature verified against the pinned update key.' -ForegroundColor Green

    $Proc = Start-Process -FilePath $InstallerPath -ArgumentList @('/S') -Wait -PassThru
    if ($Proc.ExitCode -ne 0) {
        throw "Installer exited with code $($Proc.ExitCode)."
    }
} finally {
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
}

$InstalledExe = Join-Path $env:LOCALAPPDATA 'Programs\BURNRATE\BURNRATE.exe'
Write-Host "Installed myBurnRate $Version to $InstalledExe" -ForegroundColor Green

if (-not $NoLaunch -and (Test-Path -LiteralPath $InstalledExe)) {
    Start-Process -FilePath $InstalledExe -WorkingDirectory (Split-Path -Parent $InstalledExe) | Out-Null
    Write-Host 'Launched myBurnRate.' -ForegroundColor Green
}
