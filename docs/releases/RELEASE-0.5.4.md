# BurnRate 0.5.4

A new native Windows window for BurnRate, with its own installer, dark theme, tray mode and updater, plus a round of accounting and hub fixes.

## What's new

**A native desktop window (Windows)**
- A second way to watch your usage: a small frameless window that shows the value of your sessions at published API rates, and what is left of each plan window or prepaid balance. It sits beside the existing HUD app; you can install either or both.
- **Themes:** System, Light or Dark, chosen in Settings. System follows Windows.
- **Tray:** closing the window keeps BurnRate running in the notification area, with Show, Refresh, Settings and Quit. The window remembers its position and size.
- **Alerts:** the tray warns when a prepaid balance runs low. Plan-window alerts still come from the background service. One switch in Settings controls both.
- **First run:** with nothing connected, the window shows the AI tools it found on your computer and lets you paste an API key for a balance source right there, or sign in to a hub.
- **Keyboard and motion:** every control can be reached with the keyboard, Escape closes the top layer, Alt+Up and Alt+Down reorder rows, and "reduce animations" in Windows is respected.

**Remaining usage and several accounts**
- Each row shows how much of a plan window or balance is left. Two accounts of the same tool (for example two Claude Code profiles) are separate rows with their own tag.
- Hover a row to see that day's token burn; drag rows into your own order; the window can fade when the pointer leaves it, and pinning it on top works independently of the fade.

**Hubs**
- **Sign in from Settings -> Computers.** Enter the hub address, your account and this computer's name to join "All computers" without a pairing code.
- The hub limits how much expensive read work it does at once and answers "busy, retry" instead of stalling its health check. `burnrate hub update` now requires a clean checkout, installs dependencies, checks the certificate bundle and runs a security check before restarting the service.
- A hub must run this version to feed the native window's "All computers" view.

**Fixes**
- A computer paired to a hub can edit and add plans that exist only on the hub, shows the hub's rejection or conflict instead of saving a local copy, and does not save anything while offline.
- Quarterly plans accrue their exact calendar quarter, and plan terms that have not ended respect the plan's end date.
- Timestamps outside the valid range are set aside, telemetry without a time zone is read as UTC, invalid price cards and negative usage are rejected without losing the valid rows next to them, and an explicit free one-hour cache-write rate is respected.
- A failing ingestion source no longer loses the record of its last success. A settled model correction moves tokens to the right meter.
- Muse tokens are no longer counted twice, and an idle Muse weekly window is informational until you use Muse again.
- Gemini 4 Carbon (Antigravity) is priced: $2.00 input, $0.10 cached input and $10.00 output per million tokens until 1 December 2026, then $4.00, $0.20 and $20.00.
- On Windows, the HUD app allows one instance per desktop, and clicking its shortcut brings an open window forward. The HUD app also gets the security and start-up work in the [changelog](https://github.com/kamol-nazarov/myBurnRate/blob/prod/CHANGELOG.md).

## Two Windows downloads

| System | Download | What it is |
| --- | --- | --- |
| Windows 10 or 11 (64-bit) | `myBurnRate_0.5.4_x64-setup.exe` | **The native window, new in this release.** Installs for your account only (no administrator prompt) into `%LOCALAPPDATA%\Programs\BURNRATE`, with Start Menu and Desktop shortcuts named "myBurnRate". |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.4_x64-setup.exe` | **The HUD app you may already have.** Installed copies of 0.5.0 to 0.5.3 update to this file. |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.4_aarch64.dmg` | The HUD app. |
| Linux x86-64 | `BURNRATE_0.5.4_amd64.AppImage` or `BURNRATE_0.5.4_amd64.deb` | The HUD app. |

If you are unsure, keep what you have: the HUD app update below needs nothing from you. Install the native window only if you want it. `scoop install` installs the HUD app, not the native window.

## Updating

- **Installed copies of the HUD app (0.5.0 to 0.5.3):** click the update bar in the HUD, or right-click the BURNRATE icon in the tray and choose **Install update 0.5.4 and restart**. If that item still reads **Check for updates**, click it once, wait a moment, then open the menu again. Your data stays.
- **The native window** has no earlier installer to update from: this is its first release. From the next release on, an installed copy offers the update in Settings -> General, and installs it only after you click **Install & restart**. A portable zip or source copy shows a link to the release page instead.
- **Self-hosted hubs:** update the hub too (`burnrate hub update`) to get the plan-saving fixes and to serve the native window.

## Before you install

- **Nothing is code-signed yet.** Windows SmartScreen will warn on both Windows installers, and Windows Defender may flag the native app's `BURNRATE.exe`; see the [0.5.0 notes](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.0) for opening it the first time. Every file is built by GitHub Actions from the tagged commit, has a build provenance attestation, and is listed in `SHA256SUMS`.
- **The native installer is new and has had limited testing.**
- **Your data is shared and kept.** Both Windows apps use `%LOCALAPPDATA%\BURNRATE`. Installing, updating or uninstalling the native window does not touch it, unless you tick the box on the uninstall screen to delete it, which removes it for the HUD app too.
- **What the native window sends.** An installed copy asks GitHub for `native-latest.json` once, about a minute after it starts, and then once a day: a plain HTTPS request with no usage data and no identifier beyond `BURNRATE/<version>` in the User-Agent. It downloads nothing else until you click **Install & restart**. Portable and source copies never check unless you press **Check for updates**. Set `BURNRATE_NO_UPDATE_CHECK=1` to turn every check off; see the [privacy notes](https://github.com/kamol-nazarov/myBurnRate/blob/prod/docs/privacy.md#update-checks-native-app).

The [changelog](https://github.com/kamol-nazarov/myBurnRate/blob/prod/CHANGELOG.md) lists every change.
