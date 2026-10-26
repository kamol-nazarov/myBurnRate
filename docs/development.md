# Running from source and development

Most people should install a release instead; see [Download](../README.md#download). Running from a
clone is for development, or for a Windows computer where you want the PowerShell service scripts.

## Install from a clone (Windows)

### Prerequisites

- **OS:** Windows 10 or Windows 11
- **Python:** [CPython 3.12](https://www.python.org/downloads/) (`py -3.12` or [`uv`](https://docs.astral.sh/uv/))
- **Shell:** PowerShell (`pwsh` or Windows PowerShell)

### One-Command Clone & Install

```powershell
git clone https://github.com/kamol-nazarov/myBurnRate.git
cd myBurnRate
.\install.ps1
```

Running `.\install.ps1` (or `.\scripts\install-burnrate.ps1`) automatically performs the entire installation in one step:
1. Creates the Python 3.12 `.venv` and installs the `burnrate` package.
2. Initializes the local SQLite database at `%LOCALAPPDATA%\BURNRATE\spend.db`.
3. Registers the windowless auto-start scheduled task (`BURNRATE Dashboard`) and starts the background service on `127.0.0.1:17331`.
4. Installs Desktop and Start Menu shortcuts (`BURNRATE.lnk`) with the custom high-DPI BURNRATE logo.
5. Automatically pops open the native **BURNRATE Desktop** application window (with custom taskbar & system tray icon) directly on the **Application Setup Wizard**.

<details>
<summary><strong>Manual Setup (Optional)</strong></summary>

If you prefer to create the virtual environment manually without registering the auto-start service:

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install .
burnrate init
burnrate doctor
```

</details>

Installed runs store SQLite and logs under `%LOCALAPPDATA%\BURNRATE` (`spend.db`). Set `BURNRATE_DEV=1` if you want to keep data repo-relative during development.

## Ways to run it

You can run BURNRATE in three ways depending on your workflow:

### Option A: Foreground Server + Browser

```powershell
burnrate serve
```

Then open:
- **First-Run Setup Wizard:** [http://127.0.0.1:17331/setup](http://127.0.0.1:17331/setup)
- **Sidecar HUD:** [http://127.0.0.1:17331/hud](http://127.0.0.1:17331/hud) (also [http://127.0.0.1:17331](http://127.0.0.1:17331))
- **Full Dashboard (optional):** set `BURNRATE_ENABLE_DASHBOARD=1`, restart, and open [http://127.0.0.1:17331](http://127.0.0.1:17331)

The HUD starts live quota and activity polling immediately. Its spend statistics load separately and keep usage collection running in the background, without building dashboard summaries. Local collection is unconditional: a pass runs whether or not a viewer is open, and adapters skip unchanged inputs cheaply. A HUD read only pulls the next pass forward. The HUD shows the data's age (`Updated 12s ago`) and alarms when the data or any of its polls goes quiet. On a paired spoke, the HUD shows the last hub copy at once and renews it in the background, so a slow or unreachable hub never stalls it.

### Option B: Windowless Windows Background Service (Auto-Start at Sign-In)

Use the included PowerShell lifecycle scripts to run BURNRATE silently in the background (`pythonw.exe`) and register a Windows Scheduled Task (`BURNRATE Dashboard`) that starts automatically when you sign in. Install also registers `BURNRATE Watchdog`, which runs `pythonw -m spend_app.watchdog` every 5 minutes, with no console window. It restarts the service when it stops answering or when no collection pass has finished for 10 minutes (30 for the first pass after a start), at most 3 restarts an hour,, and it leaves a service you stopped on purpose alone. Its actions are logged to `logs\watchdog.log`.

```powershell
# Register auto-start and watchdog scheduled tasks and launch in background
.\scripts\install-burnrate.ps1
.\scripts\start-burnrate.ps1

# Check live service health, PID, watchdog state, and database path
.\scripts\status-burnrate.ps1

# Restart the service (stops only the owned listener, starts it through the sign-in task)
.\scripts\restart-burnrate.ps1

# Stop the background service (the watchdog leaves it stopped until the next start)
.\scripts\stop-burnrate.ps1

# Uninstall scheduled task (keeps spend.db unless -PurgeData is passed)
.\scripts\uninstall-burnrate.ps1
```

### Option C: Native Desktop App & System Tray (`burnrate desktop`)

Launch the native Tauri v2 desktop application (which automatically supervises the local backend process, opens the main window and Always-on-Top Sidecar HUD, and docks in the Windows system tray):

```powershell
burnrate desktop
```

## Try it with made-up data

```powershell
burnrate demo
```

`burnrate demo` fills a new database in a temporary folder with a month of made-up usage, plans and
meters, and opens the HUD on [http://127.0.0.1:17377/hud](http://127.0.0.1:17377/hud). Collection,
quota polling and HUD window control are off, so nothing is collected from your tools or accounts. Pass
`--data-root DIR` to keep the demo between runs. The screenshots in the README come from it.

## Tests and front-end assets

For frontend development, edit the readable files in `frontend_src/`. Run
`npm ci --prefix scripts/asset-tools --ignore-scripts --no-audit --no-fund`, then
`node scripts/optimize-assets.cjs` to regenerate the committed `spend_web/`
assets and their checksums. Node is a development tool only; Python installs
use the committed assets. Existing public IDs and CSS classes are preserved.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

`python -m pytest` collects `tests_spend`, which includes temporary SQLite,
application-startup and browser tests as well as pure units. For focused source-evidence
tests, run `python -m pytest -q tests_spend/test_source_evidence_unit.py`.

Optional localhost bench (dev only): `scripts/Bench-Burnrate.ps1` against `http://127.0.0.1:17331`. It prints p50/p95 without a committed baseline file. Do not point it at a tailnet hostname.

See also [CONTRIBUTING.md](../CONTRIBUTING.md).
