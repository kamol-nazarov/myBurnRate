# BURNRATE v0.4.0-beta.1

BURNRATE moves to 0.4. Over the 0.3.0 betas it became a different
application: the 0.3.0 line started as a browser dashboard, and 0.4 is a
desktop HUD with multi-computer sync. This first 0.4 beta carries everything
in 0.3.0-beta.19; nothing else changes.

What 0.4 is, compared with where 0.3.0 started:

- **The HUD is the product.** A native Windows app (Tauri) puts a live
  Sidecar HUD on screen. It shows every plan window as a meter with an
  even-pace tick, tokens burned inside each window, run-out forecasts from
  quota pace or measured burn, and the data's age. The full dashboard is
  optional (`BURNRATE_ENABLE_DASHBOARD=1`).
- **Many computers, one view.** One computer is the hub; the others join with
  a pairing code and sync usage, sessions and live agents to it. No cloud
  service is involved. `burnrate hub update` updates a self-hosted hub, and the
  hub check compares builds.
- **Setup in four steps.** This computer, What we found, Plans & balances,
  and Check, with honest per-card meters, sign-in-reuse switches, one-click
  plan fixes, desktop notification switches and Copy diagnostics.
- **More plans and balances.** Claude Code (every signed-in account), Codex,
  Z.AI, OpenCode Go (every profile), Antigravity, Muse Code, Grok Build,
  OpenRouter and DeepSeek balances with a runway estimate. Cursor is gone.
- **Desktop notifications.** A plan passing its alert level or running out
  before it resets, a meter stuck for over an hour, or hub sync failing.
- **Collection that keeps up.** It never waits on a viewer, and restarts skip
  files already read. Long reads save in batches, and the desktop service runs
  collection in its own worker process. A watchdog restarts a stalled service,
  and a freshness alarm names any source or meter that stopped refreshing.

Upgrading from a 0.3.0 beta needs no migration step: the database upgrades
itself on start, and moving old de-duplication records happens in the
background. Restart BURNRATE after installing so the service and native shell
both pick up this build.

The 0.3.0-beta.19 release notes and the changelog list every change in detail.
