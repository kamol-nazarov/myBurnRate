# BurnRate 0.5.1

A hotfix for 0.5.0 on Windows.

## Fixed

- **A terminal window no longer flashes on Windows.** The installed service runs without a console, and BurnRate's setup screen asked Tailscale for this computer's network addresses through a console program started without hiding it. Each time setup loaded, a terminal window opened and closed a second later. Both Tailscale calls now run hidden. A new test fails if any background command is ever started without hiding its window.

Nothing else changes from [0.5.0](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.0).

## Updating

- **Installed copies of 0.5.0** offer this update in the HUD and the tray within a few hours. To update at once, choose **Check for updates** in the tray menu.
- **New installs** download it from the table below.
- **macOS, Linux and self-hosted hubs** are not affected by this bug, but get the same build.

| System | Download |
| --- | --- |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.1_x64-setup.exe` |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.1_aarch64.dmg` |
| Linux x86-64 | `BURNRATE_0.5.1_amd64.AppImage` or `BURNRATE_0.5.1_amd64.deb` |

This release is not yet code-signed; see the [0.5.0 notes](https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.5.0) for opening it the first time. Every file is built by GitHub Actions from the tagged commit, with a build provenance attestation and `SHA256SUMS`.
