# Contributing to myBurnRate

Thanks for helping improve a local-first cost meter for AI coding agents. Bug reports, provider
adapters, price cards, UI work, tests and documentation are all welcome.

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing you agree that your
contribution is licensed under [Apache-2.0](LICENSE) — inbound equals outbound, and no CLA is
required.

If you are reporting a vulnerability or have found a credential in the tree, stop here and follow
[SECURITY.md](SECURITY.md). Never open a public issue or pull request containing a secret, a real
prompt, a live database or a personal path.

## Before you start

- **Small fixes** — typos, docs, a broken selector, a clipped layout — can go straight to a pull
  request.
- **Anything behavioral** — a new provider, a pricing rule, a schema change, a new setting — open an
  issue first so the approach is agreed before you invest in it.
- **Version numbers, release notes and tags** belong to maintainers. Do not bump the version or edit
  `CHANGELOG.md`'s released sections in a pull request.
- **One logical change per pull request.** Do not mix formatting churn with behavior.

## Development setup

The reference contributor environment is Windows 10/11 with CPython 3.12 and PowerShell. CI runs the
test suite on Windows, macOS and Linux, so portable code is expected; macOS and Linux contributors
can run the same commands with `.venv/bin/python`.

```powershell
git clone https://github.com/kamol-nazarov/myBurnRate.git
cd myBurnRate
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m pytest
```

Optional keys in `.env` stay empty for normal development. The listener must stay on
`127.0.0.1:17331`; never bind `0.0.0.0` in tests or examples.

Useful commands while working:

```powershell
burnrate doctor                 # Python, timezone data, database, pricing, assets, loopback bind
burnrate demo                   # HUD with a month of synthetic usage, no collection
burnrate desktop --demo         # native window with the same synthetic data
burnrate desktop --check-only   # verify native widget support
```

`burnrate demo` writes to a temporary folder, never reads your tools, and never contacts a provider.
Its dataset is what the README screenshots use.

### Where things live

| Path | Contents |
| --- | --- |
| `spend_app/` | Python service, aggregation, pricing, adapters, setup wizard, HUD and the native desktop window |
| `spend_app/adapters/` | One local or API reader per source |
| `spend_web/` | Packaged dashboard and HUD assets served by the app (dashboard assets are generated from `frontend_src/`) |
| `frontend_src/` | Readable front-end sources for the dashboard |
| `pricing/` | Effective-dated YAML rate cards |
| `tests_spend/` | Hermetic pytest suite, fixtures and browser contracts |
| `packaging/` | PyInstaller, NSIS and release-manifest tooling |
| `src-tauri/` | Tauri v2 HUD shell used by the cross-platform bundles |
| `scripts/` | Install, lifecycle, validation and smoke-test scripts |
| `deploy/hub/` | Docker Compose reference deployment for a self-hosted hub |
| `docs/` | User, provider, privacy, architecture and release documentation |

## Checks to run

Run these before opening a pull request. CI enforces the same gates.

```powershell
# Full hermetic suite
.\.venv\Scripts\python.exe -m pytest

# Native window UI and layout guardrails (run after any UI edit)
.\.venv\Scripts\python.exe -m pytest tests_spend/test_wealthfront_ui.py tests_spend/test_wealthfront_layout.py -q

# Python correctness, plus lint and format on files you touched
python -m ruff check --select E9,F63,F7,F82 spend_app tests_spend
python -m ruff check spend_app\your_module.py
python -m ruff format --check spend_app\your_module.py

# Browser-marked contracts (discovers every browser test and rejects skips)
python scripts/validate-browser.py
```

The native window has hard visual rules, checked by `test_wealthfront_ui.py` and
`test_wealthfront_layout.py`:

- **No literal colors** outside `:root` and `html[data-theme="dark"]` in
  `spend_app/wealthfront_ui.html`. A new dark token must also exist in `:root`.
- **Minimum text size of 10.5 px** for all non-SVG text.
- **WCAG AA contrast of at least 4.5:1** in both Light and Dark themes.
- **No horizontal clipping or overlap** at widths `304, 320, 336, 364, 380` and `480`.

Changes to `frontend_src/` (the dashboard) must regenerate the committed assets:

```powershell
npm ci --prefix scripts/asset-tools --ignore-scripts --no-audit --no-fund
node scripts/optimize-assets.cjs
```

The HUD is edited directly in `spend_web/hud/` and does not need the optimizer. Regenerate its
standalone preview after HUD changes:

```powershell
python scripts/preview-hud.py
```

Node is a development tool only; Python installs use the committed assets. Keep existing public IDs
and CSS classes unless a change requires otherwise, and update the contract tests with them.

## Privacy rules

myBurnRate stores usage metadata only. A contribution must not introduce real telemetry, prompts,
responses, diffs, tool payloads, credentials, hostnames, Tailscale names or personal filesystem
paths.

- Do not commit `.env`, provider credential files, SQLite databases or session transcripts.
- Tests, fixtures, screenshots and docs use synthetic Northwind-style names and round numbers.
  Replace recorded usage with fixtures before opening a pull request.
- Never log credentials, never return them from the API, and never copy them into SQLite.
- Local adapters stay read-only (`mode=ro` for SQLite, no writes to harness files). Never read, reuse
  or refresh Claude's sign-in, and never write another tool's credential files.
- Opening an editor or a shell is not a live session. Active-session signals need an explicit running
  state or a documented recent-activity field.
- Redact `burnrate doctor` output and copied diagnostics before pasting them, and never paste `.env`.

## Measurement integrity

Missing data must stay missing. Do not estimate a measured value.

- Preserve token classes: fresh input, cache read, cache write, output, and reasoning when the source
  distinguishes them. Do not conflate them.
- Do not double-count reasoning tokens when the source already includes them in output.
- `exact` requires an invoice-authoritative usage source **and** an `exactness: exact` price card.
  Published-rate math is `derived` / equivalent, never a bill. Subscription proration is not provider
  billing.
- Incomplete token components must not invent a cache rate. Cache savings is null or partial when any
  cached event is unpriced or incomplete.
- Unavailable, unknown, unpriced and `$0` are distinct. Missing quotas and missing spend render as
  unavailable, never as zero.
- Keep raw provider and harness identity on stored events. Display aliases must not rewrite raw rows.
- Cross-provider duplicate suppression must be deterministic and tested.

## Provider compatibility

myBurnRate reads local files and optional documented APIs. It does not sit on the inference path.

- Do not proxy, reroute or rewrite provider requests.
- Do not add provider logos, official brand glyphs, or "official" / "certified" language.
- Prefer documented local files and official HTTPS hosts. Allowlist hosts on credential-bearing HTTP
  clients; set `trust_env=False`.
- A new adapter should fail independently. Core aggregation must not grow a new provider conditional
  for each source.
- Pay-as-you-go providers must not invent an allowance.
- Any bundled third-party asset (icon set, font, code) needs its license text in `NOTICE` and a
  compatible license.

## Experimental labels

Undocumented or unofficial interfaces stay **experimental** in code, docs and the provider matrix
until there is a documented, stable contract.

Treat at least these as experimental unless that contract exists:

- Claude Code's cached plan usage (`cachedUsageUtilization`)
- Antigravity's localhost RPC
- Traycer CLI and chat-database readers that are not a documented public API
- Grok Build log scraping

Experimental providers may break without a major version bump. Label them, isolate their failures,
and never let an experimental outage look like zero usage.

## Tests are hermetic

- No live network except `127.0.0.1`.
- No live provider credentials.
- No reads of `%USERPROFILE%` provider trees unless a test opts into a `tmp_path` home.
- No Downloads HTML, wall-clock dependence, or "this machine" observations.
- Clock, filesystem and HTTP boundaries are injected.
- Cover success, skip/unavailable, partial/unpriced, and idempotent re-ingest.

`python -m pytest` from the repository root collects only this project's suite. Tests marked
`real_db` and `browser` are excluded from the cross-platform CI job and run through their own gates.

## Adding a provider

Keep ingest, pricing, capacity and tests local. Do not validate against a live provider.

1. Add `spend_app/adapters/<name>.py` with a stable `SOURCE`, an `ingest(...)` that returns persisted
   rows or a skipped/unavailable result, and content-derived `raw_id`s. Do not retain prompt or
   response bodies.
2. Register the adapter with the provider framework (ingest, quota, activity and admin capabilities).
   Scheduler jobs iterate the registry; do not add one-off provider branches in aggregation.
3. Add `pricing/<provider>.yaml` with official `https://` `source_url` values and a file-level
   `exactness` of `exact` or `derived`. Third-party aggregators are not pricing authority.
4. If the source is invoice-authoritative, mark it as an exact usage source so `is_exact` can only be
   true when both the source and the card are exact.
5. Register display metadata. Quota collectors belong with the provider spec. Pay-as-you-go sources
   set `isPayg` and must not invent an allowance.
6. Add an offline fixture under `tests_spend/fixtures/` and tests for success, skip/unavailable,
   partial coverage and idempotent re-ingest.
7. Label the source in [docs/providers.md](docs/providers.md), the README matrix and the UI when it
   is experimental.

## Adding an effective-dated price revision

Pricing files live in `pricing/*.yaml`. Each price row needs `model_key`, per-million-token rates
(`input`, `cached_input`, `cache_write`, `output`), `effective_from`, and an `https://` `source_url`.
Optional fields include `aliases`, `effective_to`, long-context multipliers and cache-write variants.

Revisions for the same `model_key` must not overlap: close the previous card with `effective_to`
equal to the next `effective_from`. `effective_to` is exclusive. Events before the first
`effective_from` stay unpriced; do not back-date.

Official hosts used by tests include `developers.openai.com`, `platform.claude.com`, `cursor.com`,
`docs.x.ai`, `docs.z.ai`, `openrouter.ai` and `ai.google.dev`. Do not cite unofficial resellers.

`exactness: exact` is billed or invoice math. `derived` is published-rate attribution of
included-plan usage and must not be presented as a metered invoice. When a model is unpriced, store
the event as unpriced or partial; do not invent spend.

## Documentation

Docs are part of the product. A user-visible change should update the relevant page in `docs/`, the
README matrix, or both, in the same pull request.

- Keep relative links valid when renaming headings or files.
- Document behavior as it ships, including what is unavailable.
- Use synthetic data in every screenshot, and regenerate them with `burnrate demo` and
  `burnrate desktop --demo` when the interface changes.

## Commit and pull request conventions

Use [Conventional Commits](https://www.conventionalcommits.org/) with a short, lowercase scope:
`feat(desktop):`, `fix(hub):`, `docs(readme):`, `test(ui):`, `refactor(ingest):`, `chore(release):`.
The subject is imperative and under about 72 characters; the body explains why, not what.

A pull request should:

- describe the problem and the chosen approach, including alternatives you rejected;
- state how it was verified (commands, fixtures, screenshots);
- include new or updated tests, with experimental/exact/derived labels in the same change;
- pass CI, including lint, the hermetic suite, browser contracts and the native supervisor tests;
- contain no secrets, no real paths and no unrelated formatting;
- link the issue it closes, if any.

Reviewers may ask for a smaller diff. Maintainers merge once CI is green and the change has been
reviewed. Do not push to `prod`; maintainers handle release and documentation commits there.

## Questions

Product questions belong in [issues](https://github.com/kamol-nazarov/myBurnRate/issues) without
secrets. Security and conduct reports follow [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) instead.
