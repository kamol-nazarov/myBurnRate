# BurnRate 0.5.2

A hotfix for updating from the HUD.

## Fixed

- **Installing an update from the HUD works.** 0.5.0 and 0.5.1 showed the update bar, but the app refused every click on it. The app now allows the HUD to install updates, and a test fails if that permission is ever missing again.
- **A failed update can be retried.** Before, the first failed attempt removed the update and every retry failed. The HUD and the tray menu now also say why an update failed.
- **Text no longer shows through the HUD.** The card was 80% opaque, so text in the window behind it was readable through it. It is now all but opaque.
- **No more background PowerShell on Windows.** To find Antigravity, the service started a hidden PowerShell about every six seconds. It now reads the process list directly, which takes milliseconds and starts nothing.

Nothing else changes from [0.5.1](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.1).

## Updating

- **Installed copies of 0.5.0 or 0.5.1:** the update bar in the HUD can't install this update, because the bug is in the copy you have. Right-click the BURNRATE icon in the tray and choose **Install update 0.5.2 and restart**. If that item still reads **Check for updates**, click it once, wait a moment, then open the menu again. You can also run the installer below over your current copy; your data stays.
- **New installs** download it from the table below.
- **macOS, Linux and self-hosted hubs** get the same build.

| System | Download |
| --- | --- |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.2_x64-setup.exe` |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.2_aarch64.dmg` |
| Linux x86-64 | `BURNRATE_0.5.2_amd64.AppImage` or `BURNRATE_0.5.2_amd64.deb` |

This release is not yet code-signed; see the [0.5.0 notes](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.0) for opening it the first time. Every file is built by GitHub Actions from the tagged commit, with a build provenance attestation and `SHA256SUMS`.
