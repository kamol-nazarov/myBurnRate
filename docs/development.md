# Running from source and development

Most people should install a release instead; see [Install](../README.md#install). Running from a
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

### Option A: Foreground API Service

```powershell
burnrate serve
```

Starts the FastAPI service on `http://127.0.0.1:17331` serving `/api/*` and `/healthz` (non-API paths return `404` JSON with `REQ-404`). Local collection is unconditional: a pass runs whether or not the desktop window is open, and adapters skip unchanged inputs cheaply.

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

### Option C: Native usage app (`burnrate desktop`)

Open the compact Tk/ttk usage window. It starts the existing local collector,
combines paired computers, and shows API-equivalent value by tool and model.
All controls are native widgets. Use `--demo` for sample data without collection.

```powershell
burnrate desktop
```

The portable native build and its smoke test are documented in [native app](native-app.md).
`burnrate desktop` opens the native usage interface.

## Try it with made-up data

```powershell
burnrate demo
```

`burnrate demo` fills a new database in a temporary folder with a month of made-up usage, plans and
meters and serves the demo API on `127.0.0.1:17377`. Use `burnrate desktop --demo` to open the native
desktop window against that synthetic data. Collection and quota polling are off, so nothing is
collected from your tools or accounts. Pass `--data-root DIR` to keep the demo between runs.

## Tests and desktop UI checks

Edit the desktop UI in `spend_app/wealthfront_ui.html` and `spend_app/wealthfront_window.py`,
keeping existing public IDs and CSS classes covered by `tests_spend/test_wealthfront_ui.py` and
`tests_spend/test_wealthfront_layout.py`.

CI requires Python compilation and Ruff's `E9,F63,F7,F82` correctness rules across
the whole tree. Selected product modules retain their broader lint and formatting
gate. `python scripts/validate-browser.py` discovers every `browser`-marked test
and rejects skipped browser coverage; mark new browser tests so they join that gate.

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
