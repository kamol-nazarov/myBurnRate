<#
.SYNOPSIS
Fast, safe one-command production release & deploy pipeline for BURNRATE.

.DESCRIPTION
Automates the full verified release pipeline with zero manual digest copying:
  1. Runs fast local security, packaging, and secret-scan gates.
  2. Pushes release/<Version> and dispatches hub-image.yml (multi-arch candidate).
  3. Extracts the verified candidate @sha256 digest from the workflow log and pins deploy/hub/compose.yaml.
  4. Fast-forwards prod, creates annotated tag v<Version>, and pushes prod + tag.
  5. Waits for Release (parallel Windows installer + signed wheel) and Hub image promotion.
  6. Publishes the draft GitHub Release as Latest (unless -NoPublish).
  7. Optionally rolls the Hub container (-DeployHub) and/or upgrades the local desktop runtime (-UpdateDesktop).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$')]
    [string]$Version,

    [switch]$SkipLocalChecks,
    [switch]$NoPublish,
    [switch]$DeployHub,
    [string]$HubHost = "tk-server",
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

    Step "Pushing $relBranch and building Hub candidate image"
    $curBranch = (git branch --show-current).Trim()
    if ($curBranch -ne $relBranch) {
        Invoke-Checked git @("checkout", "-B", $relBranch)
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

    Step "Pinning deploy/hub/compose.yaml, merging prod, and tagging $tag"
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
    Invoke-Checked git @("checkout", "prod")
    Invoke-Checked git @("merge", "--ff-only", $relBranch)
    if (git tag -l $tag) {
        throw "Tag $tag already exists locally"
    }
    Invoke-Checked git @("tag", "-a", $tag, "-m", "BURNRATE $tag")
    Invoke-Checked git @("push", "origin", $relBranch, "prod", $tag)

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

    if (-not $NoPublish) {
        Step "Publishing GitHub Release $tag as Latest"
        Invoke-Checked gh @("release", "edit", $tag, "--draft=false", "--latest")
    }

    if ($DeployHub) {
        Step "Updating Hub server on $HubHost to $tag"
        $remoteCmd = if ($HubComposeDir) {
            "cd '$HubComposeDir' && docker pull ghcr.io/kamol-nazarov/myburnrate-hub:$Version && sed -i 's|ghcr.io/kamol-nazarov/myburnrate-hub:[^}]*|ghcr.io/kamol-nazarov/$pinRef|g' compose.yaml && docker compose up -d && sleep 2 && docker compose ps"
        } else {
            "sudo git -C /opt/burnrate fetch --tags origin && sudo git -C /opt/burnrate checkout prod && sudo git -C /opt/burnrate pull --ff-only origin prod && sudo /opt/burnrate/.venv/bin/pip install --no-deps --no-build-isolation -e /opt/burnrate -q && sudo systemctl restart burnrate-hub.service && sleep 2 && sudo systemctl is-active burnrate-hub.service && sudo /opt/burnrate/.venv/bin/python -c 'import spend_app; print(spend_app.__version__)'"
        }
        Invoke-Checked ssh @($HubHost, $remoteCmd)
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
            Invoke-Checked gh @("release", "download", $tag, "--pattern", $whlName, "--pattern", "native-latest.json", "--dir", $tmpDir)
            $feed = Get-Content -Raw -Encoding UTF8 (Join-Path $tmpDir "native-latest.json") | ConvertFrom-Json
            $whlPath = Join-Path $tmpDir $whlName
            $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $whlPath).Hash.ToLowerInvariant()
            if ($actualHash -ne $feed.wheel.sha256.ToLowerInvariant()) {
                throw "Downloaded wheel SHA-256 mismatch!"
            }

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
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmpDir
        }
    }

    Write-Host "`nDone! BURNRATE $tag is live in production." -ForegroundColor Green
} finally {
    Pop-Location
}
