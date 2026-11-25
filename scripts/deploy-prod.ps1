<#
.SYNOPSIS
Fast, safe one-command production release & deploy pipeline for BURNRATE.

.DESCRIPTION
With -PrepareCandidate, build and pin the reviewed release-branch image before
opening a pull request. After the PR is merged and the owner pushes the signed
version tag, run again to verify the release and optionally deploy it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$')]
    [string]$Version,

    [switch]$PrepareCandidate,
    [switch]$SkipLocalChecks,
    [switch]$NoPublish,
    [switch]$DeployHub,
    [ValidatePattern('^[\w.-]+$')]
    [string]$HubHost = "tk-server",
    [ValidatePattern('^(?:/[\w./-]+)?$')]
    [string]$HubComposeDir = "",
    [switch]$UpdateDesktop
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$Msg) {
    Write-Host "`n==> $Msg" -ForegroundColor Cyan
}

function Invoke-Checked([string]$Exe, [string[]]$CommandArgs) {
    & $Exe @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Exe $($CommandArgs -join ' ')"
    }
}

$repoRoot = (git rev-parse --show-toplevel).Trim()
Push-Location $repoRoot
try {
    $tag = "v$Version"
    $relBranch = "release/$Version"
    $py = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }

    Step "Validating version files for $tag"
    $initText = Get-Content -Raw -Encoding UTF8 "spend_app/__init__.py"
    if ($initText -notmatch "__version__\s*=\s*`"$([regex]::Escape($Version))`"") {
        throw "spend_app/__init__.py does not match version $Version"
    }
    $notesPath = "docs/releases/RELEASE-$Version.md"
    if (-not (Test-Path $notesPath)) {
        throw "Missing release notes file: $notesPath"
    }

    if (-not $SkipLocalChecks) {
        Step "Running local security, packaging, and secret-scan gates"
        Invoke-Checked $py @("-m", "pytest", "-q", "tests_spend/test_security_runtime.py", "tests_spend/test_packaging.py")
        Invoke-Checked $py @(".github/scripts/redacted_secret_scan.py")
    }

    if ($PrepareCandidate) {
        Step "Pushing $relBranch and building Hub candidate image"
        $curBranch = (git branch --show-current).Trim()
        if ($curBranch -ne $relBranch) {
            throw "Switch to the dedicated $relBranch branch before preparing its candidate."
        }
        Invoke-Checked git @("push", "-u", "origin", $relBranch)

        $beforeIds = @((gh run list --workflow hub-image.yml --branch $relBranch --limit 5 --json databaseId | ConvertFrom-Json) | ForEach-Object { [long]$_.databaseId })
        Invoke-Checked gh @("workflow", "run", "hub-image.yml", "--ref", $relBranch, "-f", "version=$Version")

        $candRunId = $null
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            $runs = @(gh run list --workflow hub-image.yml --branch $relBranch --limit 5 --json databaseId,event | ConvertFrom-Json)
            $fresh = $runs | Where-Object { $_.event -eq "workflow_dispatch" -and ([long]$_.databaseId -notin $beforeIds) } | Select-Object -First 1
            if ($fresh) { $candRunId = [long]$fresh.databaseId; break }
        }
        if (-not $candRunId) { throw "Could not find dispatched hub-image.yml run for $relBranch" }
        Write-Host "Watching Hub candidate run #$candRunId..."
        Invoke-Checked gh @("run", "watch", "$candRunId", "--exit-status")

        $log = (gh run view "$candRunId" --log) -join "`n"
        $pinMatch = [regex]::Match($log, "myburnrate-hub:$([regex]::Escape($Version))-rc\.(\d+)@(sha256:[0-9a-f]{64})")
        if (-not $pinMatch.Success) {
            throw "Could not parse candidate digest pin from hub-image.yml run #$candRunId"
        }
        $pinRef = $pinMatch.Value
        $rcTag = "$Version-rc.$($pinMatch.Groups[1].Value)"
        $digest = $pinMatch.Groups[2].Value
        Write-Host "Pinned candidate image: $pinRef" -ForegroundColor Green

        Step "Pinning deploy/hub/compose.yaml and preparing the release pull request"
        $composePath = Join-Path $repoRoot "deploy\hub\compose.yaml"
        $compose = Get-Content -Raw -Encoding UTF8 $composePath
        $compose = [regex]::Replace($compose, "ghcr\.io/kamol-nazarov/myburnrate-hub:[^\s}]+", "ghcr.io/kamol-nazarov/$pinRef")
        $compose = [regex]::Replace($compose, "# The v[^\r\n]+ tag promotes this candidate digest to the [^\r\n]+ image\.", "# The $tag tag promotes this candidate digest to the $Version image.")
        [IO.File]::WriteAllText($composePath, $compose, [Text.UTF8Encoding]::new($false))

        $today = (Get-Date).ToString("yyyy-MM-dd")
        $changelogPath = Join-Path $repoRoot "CHANGELOG.md"
        $changelog = Get-Content -Raw -Encoding UTF8 $changelogPath
        $updatedChangelog = [regex]::Replace($changelog, "## \[$([regex]::Escape($Version))\] - Unreleased", "## [$Version] - $today")
        if ($updatedChangelog -ne $changelog) {
            [IO.File]::WriteAllText($changelogPath, $updatedChangelog, [Text.UTF8Encoding]::new($false))
        }

        Invoke-Checked git @("add", "deploy/hub/compose.yaml", "CHANGELOG.md")
        $staged = git diff --cached --name-only
        if ($staged) {
            Invoke-Checked git @("commit", "-m", "chore(release): pin the $Version container candidate and date the changelog")
        }
        Invoke-Checked git @("push", "origin", $relBranch)
        Write-Host "Open a pull request from $relBranch to prod. After its checks and merge, sign and push $tag from the owner's environment."
        return
    }

    Step "Checking the owner-signed $tag against the published Git ref"
    Invoke-Checked git @("fetch", "--no-tags", "origin", ("{0}:{0}" -f "refs/tags/$tag"))
    Invoke-Checked git @("-c", "gpg.minTrustLevel=fully", "verify-tag", $tag)
    $expectedCommit = (git rev-parse --verify "$tag^{commit}").Trim()
    if ($LASTEXITCODE -ne 0 -or $expectedCommit -notmatch '^[0-9a-f]{40}$') {
        throw "Could not resolve the signed release tag"
    }
    $composeAtTag = (git show ("{0}:deploy/hub/compose.yaml" -f $tag)) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) { throw "Cannot read the image pin from $tag." }
    $pinPattern = 'myburnrate-hub:' + [regex]::Escape($Version) + '-rc\.\d+@sha256:[0-9a-f]{64}'
    $pins = @([regex]::Matches($composeAtTag, $pinPattern) | ForEach-Object Value | Select-Object -Unique)
    if ($pins.Count -ne 1) { throw "The signed tag must contain exactly one reviewed image pin." }
    $pinRef = $pins[0]

    Step "Waiting for Release and Hub promotion workflows on $tag"
    $relRunId = $null
    $hubTagRunId = $null
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        if (-not $relRunId) {
            $r = @(gh run list --workflow release.yml --branch $tag --limit 3 --json databaseId | ConvertFrom-Json) | Select-Object -First 1
            if ($r) { $relRunId = [long]$r.databaseId }
        }
        if (-not $hubTagRunId) {
            $h = @(gh run list --workflow hub-image.yml --branch $tag --limit 3 --json databaseId | ConvertFrom-Json) | Select-Object -First 1
            if ($h) { $hubTagRunId = [long]$h.databaseId }
        }
        if ($relRunId -and $hubTagRunId) { break }
    }
    if (-not $relRunId -or -not $hubTagRunId) {
        throw "Timed out waiting for tag workflows (release=$relRunId, hub=$hubTagRunId)"
    }
    Write-Host "Watching Hub promotion #$hubTagRunId and Release #$relRunId..."
    Invoke-Checked gh @("run", "watch", "$hubTagRunId", "--exit-status")
    Invoke-Checked gh @("run", "watch", "$relRunId", "--exit-status")
    foreach ($runId in $hubTagRunId,$relRunId) {
        $publishedCommit = (gh run view "$runId" --json headSha --jq ".headSha").Trim()
        if ($LASTEXITCODE -ne 0 -or $publishedCommit -ne $expectedCommit) {
            throw "Workflow $runId was not run at the signed release commit."
        }
    }

    if (-not $NoPublish) {
        Step "Publishing GitHub Release $tag as Latest"
        Invoke-Checked gh @("release", "edit", $tag, "--draft=false", "--latest")
    }

    if ($DeployHub) {
        Step "Updating Hub server on $HubHost to $tag"
        if ($HubComposeDir) {
            $digest = ($pinRef -split '@', 2)[1]
            $image = "ghcr.io/kamol-nazarov/myburnrate-hub@$digest"
            Invoke-Checked gh @("attestation", "verify", "oci://$image", "--repo", "kamol-nazarov/myBurnRate",
                "--signer-workflow", "kamol-nazarov/myBurnRate/.github/workflows/hub-image.yml",
                "--source-ref", "refs/tags/$tag", "--source-digest", $expectedCommit)
            $verify = ('sudo git -C /opt/burnrate fetch --no-tags origin refs/tags/{0}:refs/tags/{0} && sudo git -C /opt/burnrate -c gpg.ssh.allowedSignersFile=/etc/burnrate/release-allowed-signers -c gpg.minTrustLevel=fully verify-tag refs/tags/{0} && test "$(sudo git -C /opt/burnrate rev-parse --verify refs/tags/{0}^{{commit}})" = {1}' -f $tag,$expectedCommit)
            $remoteCmd = "$verify && cd '$HubComposeDir' && docker pull '$image' && sed -i 's|ghcr.io/kamol-nazarov/myburnrate-hub:[^}]*|ghcr.io/kamol-nazarov/$pinRef|g' compose.yaml && docker compose up -d && sleep 2 && docker compose ps"
            Invoke-Checked ssh @($HubHost, $remoteCmd)
        } else {
            Invoke-Checked $py @("-m", "spend_app", "hub", "update", "--ssh", $HubHost, "--tag", $tag, "--require-host-preflight")
        }
    }

    if ($UpdateDesktop) {
        Step "Upgrading local managed desktop runtime to $Version"
        $runtime = Join-Path $env:LOCALAPPDATA "BURNRATE\native-python"
        $pyExe = Join-Path $runtime "Scripts\python.exe"
        $pywExe = Join-Path $runtime "Scripts\pythonw.exe"
        if (-not (Test-Path $pyExe)) { throw "Managed runtime not found at $runtime" }

        $tmpDir = Join-Path $env:TEMP ("burnrate-deploy-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
        try {
            $whlName = "burnrate-$Version-py3-none-any.whl"
            Invoke-Checked gh @("release", "download", $tag, "--pattern", $whlName, "--pattern", "$whlName.sig", "--pattern", "native-latest.json", "--dir", $tmpDir)
            $feed = Get-Content -Raw -Encoding UTF8 (Join-Path $tmpDir "native-latest.json") | ConvertFrom-Json
            $whlPath = Join-Path $tmpDir $whlName
            $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $whlPath).Hash.ToLowerInvariant()
            if ($actualHash -ne $feed.wheel.sha256.ToLowerInvariant()) {
                throw "Downloaded wheel SHA-256 mismatch!"
            }

            $verifyWheel = "import sys; from pathlib import Path; from spend_app.native_update import verify_minisign_file; p=Path(sys.argv[1]); verify_minisign_file(p, Path(str(p)+'.sig').read_text(encoding='utf-8'), expected_file=p.name, expected_version=sys.argv[2])"
            Invoke-Checked $pyExe @("-I", "-c", $verifyWheel, $whlPath, $Version)
            Invoke-Checked gh @("attestation", "verify", $whlPath, "--repo", "kamol-nazarov/myBurnRate",
                "--signer-workflow", "kamol-nazarov/myBurnRate/.github/workflows/release.yml",
                "--source-ref", "refs/tags/$tag", "--source-digest", $expectedCommit)

            # Stop running desktop & service cleanly
            Get-CimInstance Win32_Process | Where-Object {
                $_.CommandLine -match "spend_app\s+desktop" -or $_.CommandLine -match "spend_app\.service\s+--port\s+17331"
            } | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 2

            Invoke-Checked $pyExe @("-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-index", "--no-deps", "--force-reinstall", "--no-input", $whlPath)
            Invoke-Checked $pyExe @("-I", "-c", "import spend_app; assert spend_app.__version__ == '$Version'")
            Start-Process -FilePath $pywExe -ArgumentList "-I -m spend_app desktop" -WorkingDirectory (Split-Path $runtime -Parent)
            Write-Host "Local BURNRATE desktop restarted on v$Version." -ForegroundColor Green
        } finally {
            if (Test-Path -LiteralPath $tmpDir) {
                $ownedTemp = Get-Item -LiteralPath $tmpDir
                $parent = (Resolve-Path -LiteralPath $env:TEMP).Path
                if ($ownedTemp.Parent.FullName -eq $parent -and
                    $ownedTemp.Name -match '^burnrate-deploy-[0-9a-f]{32}$' -and
                    -not ($ownedTemp.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    Remove-Item -LiteralPath $ownedTemp.FullName -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }

    Write-Host "`nDone! BURNRATE $tag is live in production." -ForegroundColor Green
} finally {
    Pop-Location
}
