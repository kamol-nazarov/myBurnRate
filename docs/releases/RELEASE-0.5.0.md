# BurnRate 0.5.0

**The first BurnRate you can simply download and run.** BurnRate is a desktop meter for AI coding agents: it shows what your Claude Code, Codex, Grok Build, OpenCode and other sessions are worth at published API rates, and which of your plans runs out first, in a small window that stays on top. It runs entirely on your computer, with no account, no cloud service and no analytics.

## Install

| System | Download |
| --- | --- |
| Windows 10 or 11 (64-bit) | `BURNRATE_0.5.0_x64-setup.exe` |
| macOS 11 or later, Apple Silicon | `BURNRATE_0.5.0_aarch64.dmg` |
| Linux x86-64 | `BURNRATE_0.5.0_amd64.AppImage` or `BURNRATE_0.5.0_amd64.deb` |

- **No Python, git or administrator rights needed.** The installer carries its own copy of the BurnRate service. The Windows installer installs for your account only.
- **From the command line on Windows:** `scoop install https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/myburnrate.json`
- **Updates are automatic from here on.** BurnRate checks for a new version every few hours and offers it in the HUD and the tray menu. Each update is signed and verified before it installs.

**Opening it the first time.** This release is not yet code-signed; signing through SignPath Foundation is in progress.

- **Windows:** SmartScreen may show "Windows protected your PC". Choose **More info**, then **Run anyway**.
- **macOS:** open **System Settings → Privacy & Security** and choose **Open Anyway** next to BURNRATE.
- **Linux:** make the AppImage executable first.

Every file below was built by GitHub Actions from the tagged commit, has a build provenance attestation, and is listed in `SHA256SUMS`. To check a download: `gh attestation verify <file> --repo kamol-nazarov/myBurnRate`.

## What's new

**One download, three systems**
- Native installers for Windows, macOS (Apple Silicon) and Linux.
- The app starts its own service, restarts it if it stops, and closes it when you quit.
- Start at sign-in is on by default; switch it off from the tray.

**Numbers you can trust**
- **One stated method for every figure.** Each request is valued at the provider's published list price, including long-context tiers and peak-hour pricing. Tokens and dollars always cover the same requests. When some usage has no published price, the HUD shows the day's figure as a floor (`≥`) and names what is missing. See [the methodology](https://github.com/kamol-nazarov/myBurnRate/blob/prod/docs/pricing.md#methodology).
- **Grok Build is counted once.** Earlier versions counted each Grok Build turn twice. Upgrading removes the duplicates from your history, after first saving a backup of the database.
- **New and corrected prices.** Added: GPT-6.1 Terra, GPT-6 Sol, Grok 4.7, Grok 4.7 Fast and the DeepSeek API. Claude Sonnet 5.5 cache reads are corrected to $0.10 per million tokens.

**A cleaner HUD**
- **Cost per repository:** today's value split by the repository each session ran in.
- **A shareable weekly card:** the share button copies an image of your last seven days (value against your plans, tokens, cache hit, top tools). It never includes repository names.
- **Only the tools you use:** a tool with no usage, no plan and no meter reading no longer gets an empty "quota unavailable" card.

**Run your own hub**
- A Docker image and Compose file, with HTTPS through Caddy, keep several computers in one view: `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.0`. See [self-hosting](https://github.com/kamol-nazarov/myBurnRate/blob/prod/docs/self-hosting.md).

**Privacy**
- BurnRate no longer reads or reuses Claude's sign-in. Claude meters come from the plan usage Claude Code and Claude Desktop already cache on disk.

## Upgrading

- **From an earlier source install** (a clone with `install.ps1`):
  1. Run `scripts\uninstall-burnrate.ps1`. It keeps your data.
  2. Run the installer. Your history, plans and settings in `%LOCALAPPDATA%\BURNRATE` carry over.
- **On a self-hosted hub:** update the hub as well. The hub then removes the duplicate Grok rows it received from older computers, also after a backup.

The [changelog](https://github.com/kamol-nazarov/myBurnRate/blob/prod/CHANGELOG.md) lists every change.
