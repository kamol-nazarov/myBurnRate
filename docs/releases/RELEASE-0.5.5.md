# BurnRate 0.5.5

Public release of the redesigned **myBurnRate** native Windows desktop app — featuring a compact split header, two-column provider tiles, refined controls, offline **Manrope** typography, and a one-line PowerShell installer — alongside cross-platform HUD bundles and the multi-arch Docker hub image.

## Install

### Windows 10 or 11 (Native Desktop App — Recommended)

Run in PowerShell (installs per-user with no administrator prompt, verifies size and SHA-256 against `native-latest.json`, adds Start Menu and Desktop shortcuts, and opens **myBurnRate**):

```powershell
irm https://raw.githubusercontent.com/kamol-nazarov/myBurnRate/prod/scripts/install-native.ps1 | iex
```

Or download **`myBurnRate_0.5.5_x64-setup.exe`** directly from the assets below.

### Downloads by platform

| System | Download | What it is |
| --- | --- | --- |
| Windows 10 or 11 (64-bit) | `myBurnRate_0.5.5_x64-setup.exe` | **Native desktop window (recommended on Windows).** Installs per-user into `%LOCALAPPDATA%\Programs\BURNRATE` with Light/Dark themes, tray mode, and verified in-app updates. |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.5_x64-setup.exe` | **Sidecar HUD & web dashboard.** Existing HUD 0.5.x installs and `scoop install` update to this bundle. |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.5_aarch64.dmg` | Sidecar HUD & web dashboard. |
| Linux x86-64 | `BURNRATE_0.5.5_amd64.AppImage` or `BURNRATE_0.5.5_amd64.deb` | Sidecar HUD & web dashboard. |
| Python 3.12 | `burnrate-0.5.5-py3-none-any.whl` | Python package (`burnrate serve`, `burnrate desktop`). |

## What's new in 0.5.5

- **Compact split header & two-column provider tiles:** the hero pairs your API-equivalent value with an interactive mini timeline and inline `Burn rate`, `Cache hit`, and `Value vs plans` summary strip, while provider tiles sit two-across (stacking cleanly on narrow windows) and expand full-width when clicked, with 2D drag-and-drop and `Alt+Arrow` reordering.
- **Refined controls & structured breakdown ledger:** crisp computer-scope selector, tactile segmented period tabs (`Today`, `7 days`, `Month`, `All`), and an aligned two-column token breakdown card (`Fresh input`, `Cached input`, `Output`, `Reasoning`, and `API equivalent`).
- **Offline Manrope typography:** embeds the variable `Manrope` font (`spend_app/fonts/manrope-latin.woff2`) with zero external CDN requests.
- **One-line Windows PowerShell installer:** `scripts/install-native.ps1` verifies `native-latest.json` size and SHA-256 before running the per-user installer.
- **Self-hosted Docker hub fix:** `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.5` now includes the wheel's icon assets during container builds.
- **Includes everything introduced in 0.5.4:** Light/Dark/System themes, notification-area tray mode, saved window geometry, guided first-run tool detection, passwordless hub sign-in from **Settings → Computers**, bounded hub summary reads, and verified in-app updates via `native-latest.json`.

## Updating & verification

- **Installed native app (`myBurnRate`):** open **Settings → General → Check for updates**, then click **Install & restart**.
- **Installed HUD app (0.5.0–0.5.4):** click the update bar in the HUD or choose **Check for updates** from the tray icon.
- **Self-hosted hubs:** run `burnrate hub update` or pull `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.5`.
- **SmartScreen & provenance:** Windows installers are not yet Authenticode-signed while SignPath approval is pending (choose **More info → Run anyway** on first launch). Every release asset is built on GitHub Actions from the tagged commit, listed in `SHA256SUMS`, and attested with `gh attestation verify <file> --repo kamol-nazarov/myBurnRate`.

Full details in the [changelog](https://github.com/kamol-nazarov/myBurnRate/blob/prod/CHANGELOG.md).
