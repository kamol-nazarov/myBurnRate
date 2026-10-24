# BURNRATE v0.3.0-beta.18

The HUD can no longer freeze while the underlying tools keep writing usage.
Local collection no longer waits for a viewer: a hidden window, a sleeping
laptop, a suspended tab or a wedged HUD request can stop a poll but cannot
stop ingestion. The 120-second demand lease now only pulls the next pass
forward. A stalled collection loop is always reported as an alarm instead of
being relabeled "deferred".

The HUD footer now shows the data's age (`Updated 12s ago`) and alarms when
the data is older than ten minutes, when the live-meter poll misses its
cadence, or when the recorded-usage poll goes quiet. Every HUD request aborts
after 12 seconds, so one hung socket cannot leave the UI's in-flight guard
stuck forever. Both HUD payloads carry `lastIngestAt`,
`lastIngestAgeSeconds` and `stale`.

Forecasting now has a measured-usage fallback. When a quota lane is held,
failing or stale, the forecast projects run-dry from the tokens actually
recorded in the open 5-hour or weekly window (`basis: "usage"`) instead of
disappearing. Burn velocity is the last hour of measured tokens; a quiet hour
is zero burn, never the window's historical average. The existing quota-pace
forecast still wins when it is fresh, and the safe / tight / dry warning
states are unchanged.

Provider data remains read-only: SQLite stores are still opened `mode=ro`
with `PRAGMA query_only=ON`, OpenCode still selects V2 tables with a legacy
fallback, and ZCode still introspects its columns. Cursor remains excluded.
OpenCode Go tracking is unchanged.

Verification for this release: 1102 tests pass, 5 skipped. The stale path was
reproduced on beta.17 (135 seconds with no HUD polls froze the display at
7,600 tokens while three new source events went unread) and the same sequence
on this build ingested all five events and showed 19,000 tokens with
`lastIngestAge=5s`. A six-minute soak crossed a 5-hour window boundary with
the forecast switching from measured-usage `safe` to `none` exactly at the
reset, while `lastIngestAge` stayed 0–14s.

Restart BURNRATE after installing so the service and native shell both pick
up this build.
