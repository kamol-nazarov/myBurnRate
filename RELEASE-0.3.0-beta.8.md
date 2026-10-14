# BURNRATE 0.3.0-beta.8

This release turns BURNRATE into a complete multi-computer desktop and web
application with an interactive **Application Setup Wizard**, **Single-Account
Multi-Computer Sync Hub**, and **Always-on-Top Sidecar HUD**:

- **One-Command Installer (`.\install.ps1`)**: Automatically creates the Python
  3.12 `.venv`, installs the `burnrate` package, initializes `%LOCALAPPDATA%\BURNRATE\spend.db`,
  registers the windowless auto-start scheduled task, installs Desktop & Start
  Menu shortcuts (`BURNRATE.lnk`) with the custom high-DPI BURNRATE logo, and
  automatically pops open the native desktop app window on the Setup Wizard.
- **Interactive 5-Step Setup Wizard (`/setup`)**: Automatically opens on first
  launch so you can configure computer identity,
  select multi-computer topology (**Primary Hub**, **Connected Spoke**, or
  **Standalone**), generate or paste 1-click `brpair_...` pairing codes with
  auto-detected Tailscale/LAN addresses, scan & import local AI harnesses on the
  spot, pick active monthly subscriptions, and configure the Sidecar HUD.
- **Custom Taskbar, Window & System Tray Logo**: High-resolution multi-size
  (`256×256` down to `16×16`) BURNRATE artwork embedded into `burnrate-desktop.exe`,
  window titlebars, Windows Taskbar, System Tray, and `/favicon.ico`.
- **Single-Account Multi-Computer Sync (`/api/sync/push`, `/api/machines`,
  `/api/spend/stream`)**: Stream incremental token usage, sessions, and live
  agent runs from remote computers into your primary SQLite hub with bearer
  machine tokens (`br_m_<id>_<secret>`), per-machine dashboard filtering, and
  live Server-Sent Events updates.
- **Tauri v2 Native Desktop App & Always-on-Top Sidecar HUD (`/hud`,
  `src-tauri/`)**: Native system tray application with background backend/spoke
  supervision and a frameless, always-on-top Sidecar HUD showing live 24h burn,
  active agents across computers, and subscription quota gauges.
- **Unified Dashboard & Dialog Polish**: Restored full overview, detail, and
  modal dialog layout styles within the 46 KB / 12 KB gzip CSS budget.
