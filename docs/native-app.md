# Native usage application

BURNRATE's native interface is one small desktop window built with Tk/ttk controls.
It opens directly, without a browser or embedded web renderer.
The graphite and ember palette, Segoe typography, period segments, tool marks,
and matching Settings controls use BURNRATE's existing visual identity. Initial
window sizing adapts to display scaling; the usage list scrolls at smaller sizes.

## Open it

From a Python 3.12 source install with Tcl/Tk:

```powershell
burnrate desktop
```

Try the interface with sample data and no collection:

```powershell
burnrate desktop --demo
```

`burnrate desktop --check-only` checks native widget support. `--port` selects the
local tracker port; otherwise `BURNRATE_PORT` or `17331` is used. The app starts its
local background tracker when none is running. It leaves unrelated listeners alone.
If an older tracker is already running, stop it with the existing lifecycle scripts
and reopen the new application so it can start the matching backend.

## Everyday use

- Choose **Today**, **7 days**, **This month**, or **All time**.
- The large number is the recorded usage's **API-equivalent value**. Subscription
  fees, balances, provider-reported charges, and quota percentages are excluded.
- The list shows token counts and reference value by tool. Expand a tool for its
  models; selecting a row shows fresh input, cached input, writes, output, and
  reasoning detail when recorded. Reasoning is not added to output a second time.
- Missing rates or incomplete token details remain **Unpriced**. A partly known
  total is marked as a known subtotal. A measured zero stays distinct from missing
  usage. Organization API feeds appear separately because they may overlap tool
  sessions.
- **All computers** combines paired machines. Selecting a computer filters the
  same view. Offline machines retain their last synced history. If the hub cannot
  be reached and no cached result exists, the app explicitly labels a local-only
  fallback; another computer's unknown history never becomes a measured zero.

**Settings → Sources** lists the readers on this computer and supports connecting,
changing, and disconnecting a source through the existing verified connection
service. Existing history is preserved. **Settings → Computers** lists the paired
machines and provides native pairing-code creation and joining. Generate codes on
the main computer using an address the other machines can reach. Keep pairing
codes private.

The window can stay on top. Closing it leaves the tracker collecting in the
background. Reopen BURNRATE to view usage. When started by the native app, a new
tracker uses `BURNRATE_USAGE_ONLY=1`: collection and synchronization run without
quota polling, live-agent polling, plan materialization, forecast or notification
jobs. Existing service configurations are preserved until their next restart.

An installed Python package also includes the application icons. A Python 3.12
environment with Tcl/Tk can run `pythonw -m spend_app desktop` on Windows without
the frozen executable. This is the supported source-install route when a local
security product blocks an unsigned development bundle; keep the security product
enabled rather than adding an exclusion or restoring a quarantined executable.

## Build a portable app

```powershell
python -m pip install pyinstaller
python packaging/build_native.py
```

The windowless executable is `packaging/build/native/BURNRATE/BURNRATE.exe` on
Windows. Keep its `_internal` folder beside it. The portable archive is
`packaging/build/BURNRATE-native.zip`. It contains the native interface and the
existing collector in one bundle; `BURNRATE.exe --service` runs its collector.
The frozen application is tested with `BURNRATE.exe --demo --smoke-test`, which
renders sample data and closes without reading provider sources.
`python scripts/smoke-native.py` additionally starts the bundled collector in an
isolated temporary home, verifies process ownership and real desktop IPC, and
cleans up only that test process.

The native build has a separate CI job and portable artifact. Published Tauri
installers and their update feed remain the previous release path until a native
release is explicitly prepared. The existing hub and spoke data protocol is
retained. Update the hub's code too to serve the new `/api/spend/usage` projection.
