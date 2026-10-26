# Pricing

BURNRATE never invents a dollar amount. Every figure is billed-exact, a published-rate equivalent, operator-configured subscription proration, a labeled forecast, or unavailable.

Runtime authority is the packaged YAML under `pricing/`, applied by `PricingEngine`. Cards are effective-dated. `effective_to` is exclusive (`when < effective_to`).

## Methodology

One rule produces every token count and every API-rate value (the HUD's "today", burn rate, the weekly card, the dashboard):

1. **Unit: one model request.** Claude Code and Codex record a request each; OpenCode records a step; Grok Build records each model call in `unified.jsonl` and each completed turn in `updates.jsonl`. A Grok turn counts once: the per-call lines it covers are dropped, and calls from a turn that never completed (the CLI was stopped, or the turn is still running) stay counted on their own.
2. **Tokens = fresh input + cache reads + cache writes + output.** Reasoning counts once, inside output: Claude, Codex and Grok already include it there; OpenCode Go and the DeepSeek API report it apart, so it is added.
3. **Value = the provider's published API list price for that request,** from the rate cards, with every modifier the provider publishes and the logs let us apply: long-context tiers per request (OpenAI over 272K, xAI from 200K, Claude Haiku 5.5 over 100K), cache-write durations, and peak hours (DeepSeek V4.1 Flash and V4 Pro, 01:00–04:00 and 06:00–10:00 UTC on weekdays).
4. **A provider's own charge for a request wins over the card.** That is OpenRouter's billed cost through Traycer, the admin APIs' cost reports, and Grok Build's `costUsdTicks` (1 USD = 10^10 ticks), which is Grok Build's sum of each call at xAI's list price. A Grok turn adds several calls together, so this charge is its only exact price; the card applied to the summed prompt would put the whole turn on the long-context tier.
5. **A tool's own estimate never outranks a card.** OpenCode's per-step `cost` is computed by OpenCode from its local model table, without peak hours. It is used only for a model with no card.
6. **Tokens and value cover the same requests.** A request no card can price still counts its tokens; the value is then a floor, shown as `≥` with the unpriced token count and models named.
7. **Time.** A request belongs to the instant it finished; "today" starts at local midnight.

The 2026-10-26 reconciliation that set these rules: [burnrate-pricing-reconciliation-oct26.md](burnrate-pricing-reconciliation-oct26.md).

## Price cards

Each file declares `provider`, `exactness` (`exact` or `derived`), and a list of rows:

| Field | Role |
| --- | --- |
| `model_key` | Canonical id (and optional `aliases`) |
| `input_per_mtok` / `cached_input_per_mtok` / `cache_write_per_mtok` / `cache_write_1h_per_mtok` / `output_per_mtok` | USD per million tokens |
| `long_context_threshold` plus input/output multipliers | Optional; inclusive or exclusive per provider rules |
| `peak_hours_utc` / `peak_weekdays` / `peak_multiplier` | Optional time-of-day pricing: half-open UTC hour ranges (`[[1, 4], [6, 10]]`), weekday names, and the multiplier on every token class |
| `effective_from` / `effective_to` | UTC instants |
| `source_url` | `https://` citation |
| `source_policy` | `official` (default) or `owner-asserted` (derived only) |

`source_url` must be HTTPS. Official-host prefixes include:

- `https://developers.openai.com/`
- `https://platform.claude.com/`
- `https://cursor.com/`
- `https://docs.x.ai/`
- `https://docs.z.ai/`
- `https://ai.google.dev/`
- `https://opencode.ai/`
- `https://api-docs.deepseek.com/`
- `https://openrouter.ai/` (OpenRouter’s own listings, for OpenRouter traffic only)

Third-party aggregator pages are **not** invoice authority for another provider’s bill. Requesty is forbidden as a rate source: it must not appear in `pricing/*.yaml` or `reference_rates.py`.

## Exact vs derived vs unpriced

Invoice-exact spend (`usage_events.is_exact = 1`) requires both:

1. `source` in `codex_local`, `openai_admin`, `claude_local`, `anthropic_admin`
2. A matching YAML card with `exactness: exact`

Everything else that can be priced is **derived** (dashboard `≈`). OpenRouter session cost and Z.AI credits are stored as observed numbers where the adapter has them, but computed spend still follows the YAML card’s exactness.

| State | Behavior |
| --- | --- |
| Priced + exact source + exact card | Plain currency |
| Priced + derived card | `≈` published-rate equivalent, not a bill |
| Unpriced model | Row goes to `unpriced_usage_events`; spend is not invented |
| Incomplete token classes | Coverage gap; cache rate and savings stay unavailable |
| Promo window ended | Model becomes unpriced until a new dated revision is added |

`$0` means “measured zero,” never “we do not know.” The UI uses `—` for unknown.

Owner-asserted cards (for example Codex auto-review when OpenAI has not published a page) must be `exactness: derived` and carry a `source_note`. They always render `≈`.

## What each money field means

| Field | Includes | Does not |
| --- | --- | --- |
| Priced / billed-exact | YAML cost for exact sources | Subscriptions, forecasts |
| Published-rate / API-equivalent | YAML cost for derived sources | Subscription proration |
| Tracked value / today / burn rate | Priced + published-rate **plus** subscription daily cost | Do not read this as an invoice |
| Effective cost per million tokens | Measured token classes with reliable pricing coverage | Unpriced tokens; subscriptions |
| Cache savings | Per-event no-cache counterfactual | Blended cache-hit × list price |
| Forecast | Labeled projection | Complete if pricing is incomplete — then it is a known-spend floor |

## Cache hit and cache savings

Cache hit rate = measured cache-read input ÷ measured total input. Writes and output are not in the denominator. If any contributing event has incomplete telemetry, the rate is unavailable rather than estimated.

Cache savings reprices each event with the same total input and output but **zero cache reads**, then subtracts the actual cached-input price. That is the no-cache counterfactual.

If **any** cached event in the window is unpriced or `telemetry_complete = 0`, savings is **unavailable** (`—`), not a smaller complete-looking number.

Anthropic 5-minute vs 1-hour cache writes use distinct YAML rates when the harness reports them. OpenAI cache writes follow the published 1.25× uncached-input convention on the official cards. Providers that list no cache-write fee store `0`, which is “not billed,” not “unknown.”

## Subscriptions

There is no seeded household plan table. `burnrate init` leaves `subscriptions` empty.

```powershell
burnrate subscription add --tool-key claude-code --name "Claude Code" --amount-usd 20 --cadence monthly --start-date 2026-09-01
burnrate subscription list
```

Cadences: `monthly`, `quarterly`, `annual`. Daily cost is `amount_usd / days in that period` (calendar month, calendar quarter, 365/366). That number is **your** amortization, not a provider invoice, and it is excluded from API-equivalent fields.

PAYG remaining-credit cards (OpenRouter) must not look like a plan allowance: `is_payg = 1`, no invented cap.

## Forecasts

Forecasts are labeled as forecasts. Method text names the basis (for example month-to-date published-rate equivalent). Incomplete pricing cannot produce a complete projection.

## Packaged cards (v0.1)

| File | Exactness | Typical use |
| --- | --- | --- |
| `pricing/openai.yaml` | exact | Codex Desktop / OpenAI admin |
| `pricing/anthropic.yaml` | exact | Claude Code / Anthropic admin |
| `pricing/openai-auto-review.yaml` | derived, owner-asserted | Codex auto-review when no official page exists |
| `pricing/openai-daybreak.yaml` | derived, owner-asserted | Codex CLI Daybreak preview id (Sol rates until OpenAI publishes a page) |
| `pricing/cursor.yaml` | derived | Cursor model rates, kept only so an older backup can still be read; Cursor usage is no longer collected or counted |
| `pricing/google.yaml` | derived | Antigravity Gemini API-equivalent; not an Antigravity bill |
| `pricing/xai.yaml` | derived | SuperGrok / Grok Build at published xAI API rates |
| `pricing/zai.yaml` | derived | OpenCode / ZCode Coding Plan attribution (credits ≠ invoice USD) |
| `pricing/openrouter.yaml` | derived | OpenRouter-listed models; promo rows close on `effective_to` |

Rates change. Read the YAML `source_url` rather than copying numbers out of this document.

Beta.6 recognizes current Cursor model effort/speed identifiers, OpenCode
sessions whose recorded provider is OpenAI, xAI or Cursor, and ZCode GLM-5.3
Coding Plan usage. Those remain published-rate equivalents, not subscription
invoices. DeepSeek Flash and OpenRouter DeepSeek V4.1 Flash remain unpriced:
their published rates vary by UTC time window, which this date-based card format
cannot express without inventing a rate for peak traffic.

Antigravity / Gemini attribution cites Google’s published API pricing (`https://ai.google.dev/gemini-api/docs/pricing`), not a third-party listing, and does not treat aggregator cache-write SKUs as Google storage fees. Cursor model rows follow Cursor’s own usage-rate table.

Direct xAI has a derived card so diagnostics can say “no telemetry”; there is no ingest adapter.

## Adding a dated revision

1. Confirm an official `https://` source (or `owner-asserted` + `source_note` for derived-only).
2. Add a new row with `effective_from` set to the rate-change instant. Close the previous row with exclusive `effective_to`.
3. Do not silently extend a promo: when the window ends, omit a successor so events become unpriced.
4. Cover the boundary with a hermetic fixture test. Do not call live pricing endpoints from tests.

Contributor detail lives in [CONTRIBUTING.md](../CONTRIBUTING.md).
