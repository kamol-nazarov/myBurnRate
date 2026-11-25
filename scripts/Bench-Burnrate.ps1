[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:17331',
    [string]$KeyPath,
    [ValidateRange(2, 1000)]
    [int]$Requests = 20,
    [string]$Baseline
)

# Measures the BURNRATE JSON API. Read-only: summary and health only.
# A baseline JSON is optional; without it the script still prints p50/p95.

$ErrorActionPreference = 'Stop'
$origin = [Uri]$BaseUrl
if (-not $origin.IsLoopback -or $origin.Scheme -notin 'http', 'https' -or $origin.UserInfo -or
    $origin.AbsolutePath -ne '/' -or $origin.Query -or $origin.Fragment) {
    throw 'Benchmark credentials are only sent to a loopback origin.'
}
$BaseUrl = $origin.GetLeftPart([UriPartial]::Authority)
if (-not $Baseline) {
    $Baseline = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\perf-baseline.json'
}
if (-not $KeyPath) {
    . (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
    $database = if ($env:SPEND_DATABASE_PATH) { $env:SPEND_DATABASE_PATH } else { Get-BurnrateDbPath }
    $KeyPath = Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($database))) '.burnrate-auth\access.key'
}
$bearer = (Get-Content -Raw -LiteralPath $KeyPath).Trim()
if ($bearer -cnotmatch '^[a-f0-9]{64}$') {
    throw 'The local credential file has an invalid format.'
}
$limits = $null
if (Test-Path -LiteralPath $Baseline) {
    $limits = Get-Content -Raw -LiteralPath $Baseline | ConvertFrom-Json
}

Add-Type -AssemblyName System.Net.Http
$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect = $false
$handler.UseProxy = $false
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(30)
$client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $bearer)

function Measure-ResponseBytes([string]$Uri, [string]$Encoding) {
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Uri)
    $reply = $null
    try {
        $request.Headers.AcceptEncoding.ParseAdd($Encoding)
        $reply = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $reply.IsSuccessStatusCode) {
            throw "BURNRATE benchmark request failed (HTTP $([int]$reply.StatusCode))."
        }
        $bytes = $reply.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        return $bytes.Length
    } finally {
        if ($reply) { $reply.Dispose() }
        $request.Dispose()
    }
}

function Measure-Endpoint([string]$Uri, [int]$Count) {
    $samples = @()
    for ($i = 0; $i -lt $Count; $i++) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $null = Measure-ResponseBytes $Uri 'gzip'
        $sw.Stop()
        $samples += $sw.Elapsed.TotalMilliseconds
    }
    # The first request after a data change is the cold computation; the bench
    # measures the steady state every later poll pays.
    $warm = @($samples | Select-Object -Skip 1 | Sort-Object)
    [pscustomobject]@{
        cold = [math]::Round($samples[0], 1)
        p50  = [math]::Round($warm[[math]::Floor(($warm.Count - 1) * 0.50)], 1)
        p95  = [math]::Round($warm[[math]::Floor(($warm.Count - 1) * 0.95)], 1)
        max  = [math]::Round(($warm | Measure-Object -Maximum).Maximum, 1)
    }
}

try {
    $failures = @()
    $rows = @()
    $windows = if ($limits -and $limits.summary_p95_ms) {
        @($limits.summary_p95_ms.PSObject.Properties.Name)
    } else {
        @('15m', '1h', '1d', '1w')
    }
    foreach ($window in $windows) {
        $result = Measure-Endpoint "$BaseUrl/api/spend/summary?window=$window&tool=all" $Requests
        $limit = if ($limits) { $limits.summary_p95_ms.$window } else { $null }
        $rows += [pscustomobject]@{
            endpoint = "summary $window"
            cold_ms = $result.cold
            p50_ms = $result.p50
            p95_ms = $result.p95
            max_ms = $result.max
            limit_ms = $limit
        }
        if ($null -ne $limit -and $result.p95 -gt [double]$limit) {
            $failures += "summary $window p95 $($result.p95) ms > $limit ms"
        }
    }
    $health = Measure-Endpoint "$BaseUrl/api/spend/health" $Requests
    $healthLimit = if ($limits) { $limits.health_p95_ms } else { $null }
    $rows += [pscustomobject]@{
        endpoint = 'health'
        cold_ms = $health.cold
        p50_ms = $health.p50
        p95_ms = $health.p95
        max_ms = $health.max
        limit_ms = $healthLimit
    }
    if ($null -ne $healthLimit -and $health.p95 -gt [double]$healthLimit) {
        $failures += "health p95 $($health.p95) ms > $healthLimit ms"
    }
    $rows | Format-Table -AutoSize | Out-String -Width 160

    $summaryUri = "$BaseUrl/api/spend/summary?window=15m&tool=all"
    $gzipBytes = Measure-ResponseBytes $summaryUri 'gzip'
    $rawBytes = Measure-ResponseBytes $summaryUri 'identity'
    $summaryLimit = if ($limits) { $limits.summary_gzip_bytes_max } else { $null }
    [pscustomobject]@{
        endpoint = 'summary 15m'
        gzip_bytes = $gzipBytes
        raw_bytes = $rawBytes
        limit_bytes = $summaryLimit
    } | Format-Table -AutoSize | Out-String -Width 120
    if ($null -ne $summaryLimit -and $gzipBytes -gt [int]$summaryLimit) {
        $failures += "summary 15m gzip $gzipBytes B > $summaryLimit B"
    }

    if ($failures.Count -gt 0) {
        Write-Host "PERF REGRESSION: $($failures -join '; ')" -ForegroundColor Red
        exit 1
    }
    Write-Host 'Perf within baseline.' -ForegroundColor Green
} finally {
    $client.Dispose()
}
