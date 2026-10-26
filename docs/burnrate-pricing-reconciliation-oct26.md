# BurnRate pricing reconciliation: Monday, October 26, 2026

Two figures for the same morning disagreed. An hourly harness table summed 12 AM–1 PM to **$464.10 on about 2.88B tokens**. BurnRate's HUD card at about 2:47 PM said **$427.23 since midnight on 3.77B tokens** (98% cache hit, Antigravity feed flagged stale). BurnRate counted more tokens and fewer dollars.

This document recomputes the day from the raw logs, shows where the counting and the pricing diverged, and states the methodology BurnRate now follows. All times are America/New_York (EDT, UTC−4). "Midnight" is 04:00 UTC.

## Corrected totals

| Window | Value at published API rates | Tokens | Of which NOT VERIFIED |
| --- | --- | --- | --- |
| 12:00 AM – 1:00 PM | **$779.20** | **2,981,480,857** | $12.65 on 12,567,576 tokens |
| 12:00 AM – 2:47 PM | **$851.98** | **3,545,067,344** | $12.74 on 12,630,207 tokens |

The verified floors are $766.55 (to 1 PM) and $839.24 (to 2:47 PM). Antigravity is not included; see [NOT VERIFIED](#not-verified).

Both totals come from two independent computations that agree:

1. Scripts that read the raw logs directly and price each request from the official pages, without BurnRate's code.
2. The fixed BurnRate run over a copy of today's database.

Midnight to 1 PM, they agree to $0.01 and 150,053 tokens. The difference is one OpenCode step that started at 10:54 PM yesterday and finished at 12:02 AM. BurnRate dates it by when it finished, which is the rule everywhere else; the scripts had filtered by start time. To 2:47 PM, the fixed BurnRate is $0.10 higher than the raw recompute: that one step's $0.01 plus $0.09 of `codex-auto-review` at an owner-asserted rate (NOT VERIFIED).

### By tool, 12:00 AM – 1:00 PM

| Tool / model | Requests | Tokens | Value | Rule that matters |
| --- | --- | --- | --- | --- |
| **Claude Code** | 1,947 | 786,684,885 | **$222.28** | |
| Claude Opus 5.5 | 1,539 | 704,876,088 | $211.08 | all cache writes 1-hour |
| Claude Sonnet 5.5 | 192 | 27,914,486 | $6.71 | cache hit $0.10/M |
| Claude Haiku 5.5 | 216 | 53,894,311 | $4.48 | 210 requests over 100K pay the higher tier |
| **Codex** | 3,477 | 1,069,481,854 | **$349.95** | |
| GPT-6 Sol | 2,182 | 598,530,583 | $217.92 | 1,156 requests over 272K input |
| GPT-6.1 Sol | 1,295 | 470,951,271 | $132.03 | 832 requests over 272K input |
| **Grok Build** | 74 turns + 225 calls | 303,439,711 | **$200.01** | |
| Completed turns (Grok Build's own charge) | 74 | 278,155,921 | $178.38 | `costUsdTicks` |
| Calls in turns that never completed, still in the log | 106 | 12,716,214 | $8.98 | per call, at the Grok 4.7 / 4.7 Fast card |
| Calls in turns that never completed, only in BurnRate's database | 119 | 12,567,576 | $12.65 | NOT VERIFIED |
| **OpenCode** | 3,709 steps | 821,874,407 | **$6.96** | |
| OpenCode Go, DeepSeek V4.1 Flash | 2,807 | 629,754,375 | $5.54 | 684 steps in peak hours |
| OpenCode Go profile 2, DeepSeek V4.1 Flash | 90 | 18,751,270 | $0.13 | |
| DeepSeek API, `deepseek-flash` | 812 | 173,368,762 | $1.29 | 433 steps in peak hours; reasoning billed as output |
| **Antigravity** | — | — | NOT VERIFIED | no local log; BurnRate has never recorded any |
| **Total** | | **2,981,480,857** | **$779.20** | |

### By tool, 12:00 AM – 2:47 PM

| Tool | Tokens | Value |
| --- | --- | --- |
| Claude Code | 932,004,429 | $261.89 (Opus 5.5 $250.70) |
| Codex | 1,179,325,065 | $379.90 (includes $0.09 `codex-auto-review`, NOT VERIFIED) |
| Grok Build | 303,668,807 | $200.92 (includes $12.65 NOT VERIFIED) |
| OpenCode | 1,130,069,043 | $9.26 |
| **Total** | **3,545,067,344** | **$851.98** |

## The three figures side by side

| | 12 AM – 1 PM | 12 AM – 2:47 PM |
| --- | --- | --- |
| Hourly harness table | $464.10 on ~2.88B | — |
| BurnRate card (served by the hub) | — | $427.23 on 3.77B |
| BurnRate before the fix, this PC's data | $362.57 on 3,255,082,742 | $434.44 on 3,818,898,325 |
| **Corrected** | **$779.20 on 2,981,480,857** | **$851.98 on 3,545,067,344** |

Both earlier figures were too low.

| Tool, to 1 PM | Table | BurnRate before | Corrected |
| --- | --- | --- | --- |
| Claude Code | $192.77 | $224.97 | $222.28 |
| Codex | $154.52 | $132.03 | $349.95 |
| Grok Build | $109.09 | $0.00 | $200.01 |
| OpenCode | $7.70 | $5.58 | $6.96 |

I could not reproduce the table. None of its per-tool figures matches the raw logs under the published rates or under any of these variations:

| Variation tried | Result | Table's figure |
| --- | --- | --- |
| Codex without the long-context tier | $207.09 | $154.52 |
| GPT-6 Sol at GPT-6.1 Sol's rates, no tier | $147.56 | $154.52 |
| Every Grok turn at the short Grok 4.7 rates | $167.16 | $109.09 |
| Claude's 1-hour cache writes at the 5-minute rate | $207.42 | $192.77 |

Its source and method are NOT VERIFIED.

## Where the counting diverged

**1. Grok Build was counted twice.**
- **The two logs:** Grok Build logs every model call to `~/.grok/logs/unified.jsonl` and every completed turn to `~/.grok/sessions/<cwd>/<session>/updates.jsonl`. BurnRate stored both.
- **Why the duplicates survived:** its reconcile step only recognised a turn when one log line had the same timestamp and token counts. Today's log lines are single calls, so a turn (the sum of 1–248 calls) never matched one.
- **Evidence:** for 81 of the day's 84 turns, the stored per-call rows add up exactly to the turn record. For example, session `01a17824-99d5` has 286 calls and 3 turns, both totalling 36,400,816 input tokens.
- **Size:** to 1 PM, BurnRate held 577,243,819 Grok tokens where 303,439,711 were real; 273,804,108 were duplicates.
- **History:** across all days, the copy of this PC's database held 11,657 duplicate per-call Grok rows.

**2. DeepSeek API reasoning was dropped.**
- **What happened:** OpenCode stores reasoning apart from output for the DeepSeek API, as it does for OpenCode Go. BurnRate added it back only for OpenCode Go.
- **Evidence:** OpenCode's own cost for all 598 of today's DeepSeek API steps that had reasoning reproduces from DeepSeek's rates only with reasoning billed as output.
- **Size:** 202,223 tokens.

**3. Tokens and dollars covered different requests.**
- **What happened:** BurnRate's "tokens today" counted every stored request. "Value today" counted only requests a rate card could price.
- **Size:** at 1 PM, 1,175,774,402 of BurnRate's 3.26B tokens had no price: 598,530,583 GPT-6 Sol and 577,243,819 Grok.
- **How it showed:** they raised the token total and added nothing to the dollar total. That is the "more tokens, fewer dollars" pattern.
- **Why it looked complete:** the HUD showed the partial dollar figure as a plain number; only a tooltip said "Partial".

**Checked and not a problem:**
- **Claude Code transcripts:** BurnRate's database held exactly the requests and tokens the raw transcripts hold, model by model: 1,539 / 192 / 216 requests. That includes Claude Desktop workspace sessions, whose file paths pass Windows' 260-character limit.
- **Codex:** BurnRate matched the raw rollout files exactly for GPT-6.1 Sol and GPT-6 Sol. Two independent methods (fork-aware replay skipping, and event-identity deduplication) found the same 2,182 GPT-6 Sol requests. They come from a ModernWars session and about 35 sub-agent sessions it started between midnight and 5:45 AM.
- **Fast mode and US-only inference:** none of today's 2,537 Claude requests used either (`speed: standard`, `inference_geo: global` on every one).
- **ZCode, Muse and Traycer:** no usage today. ZCode's last request was at 7:26 PM on October 25; Traycer's last record is from September 26.

## Where the pricing diverged

To 1 PM, BurnRate's $362.57 becomes $779.20:

| Change | Effect to 1 PM |
| --- | --- |
| GPT-6 Sol had no rate card | +$217.92 |
| Grok 4.7 and Grok 4.7 Fast had no rate card, and Grok Build's own per-turn charge was ignored | +$200.01 |
| Claude Sonnet 5.5's card had the cache-hit price at $0.20/M; the published price is $0.10/M | −$2.69 |
| OpenCode's own cost estimate outranked the card, and the estimate ignores DeepSeek's published peak hours | +$0.98 (OpenCode Go) |
| DeepSeek API steps: peak hours, plus the reasoning tokens in item 2 above | +$0.40 |
| **Total** | **+$416.63** |

The card at 2:47 PM came from the hub. BurnRate's code over this PC's data gives $434.44 on 3.82B tokens at 2:47 PM. That is $7.21 and about 50M tokens more than the card, consistent with a few minutes of sync lag before this PC's newest events reached the hub. The hub's state at 2:47 PM cannot be replayed, so this step is NOT VERIFIED.

## Rates used

Every rate was read from the provider's own page on 2026-10-26. The figures are per million tokens.

| Model | Input | Cache write | Cache hit | Output | Modifiers | Source |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | $4 | $5 (5 min), $8 (1 h) | $0.20 | $20 | 1M context at standard price. Fast mode $8/$40, not used today. `inference_geo: us` 1.1×, not used today. | [platform.claude.com pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Claude Sonnet 5.5 | $2 | $2.50, $4 | $0.10 | $10 | Cache hit is 0.05× input (footnote 2) | same |
| Claude Haiku 5.5 | $0.10 | $0.125, $0.20 | $0.01 | $0.50 | Prompt over 100,000 tokens (input, cache reads and cache writes): $0.50 / $0.625, $1 / $0.05 / $2.50 | same |
| GPT-6.1 Sol | $2 | $2.50 | $0.10 | $10 | More than 272K input tokens: 2× input and cache, 1.5× output, whole request | [GPT-6.1 Sol model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol) |
| GPT-6 Sol | $2 | $2.50 | $0.20 | $10 | Same long-context rule | [GPT-6 Sol model page](https://developers.openai.com/api/docs/models/gpt-6-sol) |
| Grok 4.7 (Grok Build: `grok-4.7-build`) | $2 | none | $0.50 | $6 | From 200K prompt tokens: $4 / $1 / $12 | [xAI pricing](https://docs.x.ai/developers/pricing) |
| Grok 4.7 Fast (Grok Build: `grok-4.7-build-fast`) | $4 | none | $1 | $12 | Above 200K: $6 / $1.50 / $18. Offered only in Cursor and Grok Build. | same |
| DeepSeek V4.1 Flash on OpenCode Go | $0.15 | none | $0.003 | $0.60 | Peak (01:00–04:00 and 06:00–10:00 UTC, Monday–Friday): $0.30 / $0.006 / $1.20 | [OpenCode Go](https://opencode.ai/docs/go/) |
| DeepSeek API `deepseek-flash` (DeepSeek-V4.1-Flash-1017) | $0.15 | none | $0.003 | $0.60 | Same peak rule; the page also makes Chinese public holidays off-peak. October 26 is not one. | [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) |

How the less obvious ones were checked against the logs:

- **Grok Build's own charge.** Each completed turn records `costUsdTicks`.
  - Two turns reproduce exactly from the Grok 4.7 rates at 10¹⁰ ticks per dollar. One has 19 calls: 13,188,320,000 ticks = $1.318832. The other is a single call of 203,612 prompt tokens: 2,185,040,000 ticks = $0.218504, the long-context tier.
  - So the ticks are the list price of each call at its own tier.
  - This also verifies the model mapping: `grok-4.7-build` bills as Grok 4.7, and `grok-4.7-build-fast` bills at exactly 2× Grok 4.7 below 200K, which is Grok 4.7 Fast's rate.
- **OpenCode's `cost` field.** For every one of today's 4,928 DeepSeek V4.1 Flash steps it equals the off-peak rates, including the 684 steps that ran in published peak hours. It is OpenCode's own estimate, not a charge.
- **Long-context tiers are applied per request.** Codex and Claude log one row per request. Grok's turn records add several calls together, which is why the provider's per-turn charge, not the card, prices a turn.

## NOT VERIFIED

1. **Grok calls that only BurnRate saw: 119 calls, 12,567,576 tokens, $12.65.**
   - **What they are:** these turns never completed (the CLI was stopped or the session ended), so no turn record exists.
   - **Why they can't be checked:** the CLI has since trimmed `unified.jsonl` (it now starts at 11:33 AM), so the per-call lines exist only in BurnRate's database, which read them before the trim.
   - **Sessions:**
     - `01a17659-8eb4` 19 calls
     - `01a1774b-f1bc` 32
     - `01a1774c-4d5e` 22
     - `01a1774c-9b7d` 20
     - `01a1774c-bcf6` 17
     - `01a176ad-b6c8` 9
2. **Antigravity.**
   - BurnRate reads Antigravity through its localhost language server, not a log file. Its collector reports success but has never stored a row.
   - Today's Antigravity usage is unknown and not in any total here.
3. **Whether OpenCode Go and the DeepSeek API actually charge the published peak price.** The published rule is applied. Nothing local shows what was deducted: OpenCode's field is an estimate. Affects $0.98 and $0.40.
4. **Grok 4.7 Fast above 200K prompt tokens.** The rate is the published one, but no call today reached that tier.
5. **`codex-auto-review`: $0.09 by 2:47 PM.** Its card is owner-asserted from a third-party page; OpenAI publishes no price.
6. **The model of 64 Grok calls logged without one** (in the trimmed log's `unknown` scope). They are priced as Grok 4.7, the model every completed turn in their sessions (`01a17659-8eb4`, `01a1774b-f1bc`) names. Value: $5.18.
7. **The hub's 2:47 PM card** (see above) and **the hourly table's method**.

## Methodology BurnRate now follows

Stated in full in [pricing.md, "Methodology"](pricing.md#methodology). In short:

- **Unit:** one model request.
- **Tokens:** fresh input + cache reads + cache writes + output, with reasoning counted once.
- **Value:** the provider's published list price for each request, with its published long-context and peak-hour rules.
- **Reported charges:** a provider's own charge for a request wins over the card. A tool's own estimate never does.
- **Grok:** a turn counts once.
- **Same requests on both sides:** tokens and value cover the same requests. Anything no card can price is shown as a floor (`≥`), with its tokens named.

## Fixes

Branch `fix/pricing-reconciliation` of `kamol-nazarov/myBurnRate`, in the worktree `C:\Dev\myBurnRate-pricing-fix`.

| Problem | Fix |
| --- | --- |
| Grok counted twice | `covered_calls` matches each completed turn to the run of calls that ends at it and adds up to its input and output; those calls are dropped, and calls of turns that never completed stay (`spend_app/adapters/grok_native.py`). Runs at local ingest, on the hub for every pushed batch (`spend_app/sync.py`), and once per database at start-up with a verified backup first (`dedupe_grok_calls` in `spend_app/db.py`). |
| Grok's charge ignored | Turn rows carry `costUsdTicks` as `cost_usd`. A turn stored without it is re-inserted, so the corrected row syncs to the hub again. |
| Missing cards | `gpt-6-sol` (`pricing/openai.yaml`); `xai:grok-4.7` and `xai:grok-4.7-fast` with the Grok Build names as aliases (`pricing/xai.yaml`); `deepseek:deepseek-flash` (new `pricing/deepseek.yaml`). Each starts 2026-10-26, the day its price was verified, so earlier days stay marked partial unless the row carries a provider charge. |
| Sonnet 5.5 cache hit | $0.20 → $0.10 (`pricing/anthropic.yaml`). |
| Peak hours | Cards gain `peak_hours_utc`, `peak_weekdays` and `peak_multiplier` (`spend_app/pricing.py`); the DeepSeek cards use them. |
| Estimates outranking cards | `ESTIMATED_COST_SOURCES` in `spend_app/aggregate.py`: OpenCode's estimate is used only when no card prices the model, at every place that values usage. |
| DeepSeek reasoning | Counted as output (`spend_app/adapters/opencode_schema.py`). |
| Partial figure shown as complete | The HUD prefixes `≥` and its tooltip names the unpriced tokens and models (`spend_app/hud.py`, `spend_app/hud_data.py`). |

Tests: `tests_spend/test_pricing_reconciliation.py` encodes this day: the peak-hour prices, both Grok charges, the interrupted-turn case, the hub push, the one-time repair and the value rule. The full suite passes: 1,192 passed, 5 skipped.

## Rolling it out

Nothing is merged, deployed or restarted yet.

1. **This PC.**
   - Merge the branch into the checkout the desktop service runs from, then restart the service.
   - On start-up it backs up `spend.db` to `backups\pre-grok-dedupe-*.db`, removes the duplicate Grok rows, and re-reads Grok and OpenCode. Turn rows then gain Grok Build's charge and are pushed to the hub again.
   - Note: the "BURNRATE Dashboard" task runs `python -m spend_app.service` with `C:\Dev\myBurnRateOfficial` as its working directory, so it imports whatever that development checkout holds, even modules loaded mid-run. An installed copy would be safer.
2. **The hub.**
   - Deploy the same change to `release/0.4.0-beta.1` (the hub tracks that branch of `kamol-nazarov/myBurnRate`). The hub runs the same backup and dedupe on `/var/lib/burnrate/burnrate_hub.db`.
   - The HUD card reads from the hub, so it stays wrong until the hub is updated.
   - The Grok cards and the dedupe must ship together. With only the cards, the hub would value its duplicate Grok rows twice.
3. **`kamol-nazarov/burnrate`** is the older repository and nothing deploys from it; it needs no change.

## State of the tree

- **Working tree:** before any work, `C:\Dev\myBurnRateOfficial` was clean at `6d5b5ed` on `feat/public-release`, with no deleted or modified files.
- **Data:** `%LOCALAPPDATA%\BURNRATE` held `spend.db` (533 MB) and its logs.
- **Method:** the analysis used a read-only snapshot of that database taken at 3:40 PM.
