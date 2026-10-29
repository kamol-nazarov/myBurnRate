# BurnRate 0.5.3

A hotfix for Grok usage that lost its price.

## Fixed

- **Grok calls stay priced when Grok trims its log.** The Grok CLI cuts the start of its log, which held the line naming each session's model. Calls BurnRate had already priced were then read again without a model and counted as "unknown": the HUD showed the day's value as a floor, with "tokens have no published price (unknown)". A call keeps the model it was saved with, and a call that never had one takes the model its Grok session records. Calls already saved as unknown are priced on the first pass after updating.

Nothing else changes from [0.5.2](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.2).

## Updating

- **Installed copies of 0.5.2:** click the update bar in the HUD.
- **Installed copies of 0.5.0 or 0.5.1:** right-click the BURNRATE icon in the tray and choose **Install update 0.5.3 and restart**. If that item still reads **Check for updates**, click it once, wait a moment, then open the menu again. You can also run the installer below over your current copy; your data stays.
- **New installs** download it from the table below.
- **macOS, Linux and self-hosted hubs** get the same build. A hub does not need this update to show the corrected figures: the computer that runs Grok sends them.

| System | Download |
| --- | --- |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.3_x64-setup.exe` |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.3_aarch64.dmg` |
| Linux x86-64 | `BURNRATE_0.5.3_amd64.AppImage` or `BURNRATE_0.5.3_amd64.deb` |

This release is not yet code-signed; see the [0.5.0 notes](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.0) for opening it the first time. Every file is built by GitHub Actions from the tagged commit, with a build provenance attestation and `SHA256SUMS`.
