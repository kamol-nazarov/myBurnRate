# Install BURNRATE Python deps for CI. No provider keys. Repo-local only.
# Dependencies come from the hash-locked exports of uv.lock; the project itself
# is installed with --no-deps so nothing resolves outside the lock.
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath "requirements-dev.txt")) {
    Write-Error "requirements-dev.txt is missing; run: uv lock then uv export --frozen --extra dev"
    exit 1
}

python -m pip install --require-hashes -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m pip install --no-deps --no-build-isolation -e .
exit $LASTEXITCODE
