# BURNRATE v0.3.0-beta.17

"Today" now means the local calendar day on every computer, hub included.
When `SPEND_TIMEZONE` is unset the configured timezone resolves to the
machine's own IANA zone instead of UTC; before, an evening in New York
showed only the usage after 00:00Z (20:00 local), so a full day read as a
few dollars.

Interactive Codex CLI sessions count again: current CLI builds write
originator `codex-tui`, which the rollout reader rejected as unsupported
and silently dropped from the ledger.

The HUD's recorded usage no longer flips between this computer and the
hub. A spoke keeps the last hub copy across a sync and answers a HUD poll
at once while that copy renews, held figures are kept per computer, and a
window-token total follows a row whose tokens were revised in place.

Also since beta.16: quota-window token burn on HUD meter hover (weekly
meters list today's tokens by model), rebuilt HUD tooltips, a transparent
native HUD window, animated collapse/expand, and Z.AI quota readings that
recover after a key change.

Update the hub as well as each computer, then restart BURNRATE and reopen
the HUD. The broader suite still has the same 23 failures reproduced on
the previous commit; none are new.
