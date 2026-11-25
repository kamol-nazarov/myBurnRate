$ErrorActionPreference = 'Stop'

if (-not $env:RUNNER_TEMP -or -not $env:GITHUB_ENV) {
    throw 'NSIS setup requires the GitHub Actions temporary directory and environment file.'
}

# NSIS project distribution and published SHA-256:
# https://sourceforge.net/projects/nsis/files/NSIS%203/3.13/nsis-3.13.zip/download
$version = '3.13'
$expected = 'ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8'
$workspace = Join-Path $env:RUNNER_TEMP ("burnrate-nsis-" + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $workspace
$archive = Join-Path $workspace "nsis-$version.zip"
$uri = "https://downloads.sourceforge.net/project/nsis/NSIS%203/$version/nsis-$version.zip"
curl.exe --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
    --connect-timeout 10 --max-time 45 --retry 2 --retry-delay 4 --output $archive $uri
if ($LASTEXITCODE -ne 0) {
    throw "Could not download the pinned NSIS $version archive."
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
    throw "NSIS $version archive checksum mismatch."
}

Expand-Archive -LiteralPath $archive -DestinationPath $workspace
$compiler = Join-Path $workspace "nsis-$version/makensis.exe"
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw "NSIS $version archive does not contain makensis.exe at the expected path."
}
$env:BURNRATE_MAKENSIS = $compiler
Add-Content -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Value "BURNRATE_MAKENSIS=$compiler"
Write-Output "Prepared checksum-verified NSIS $version in the runner's temporary directory."
