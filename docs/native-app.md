# Native usage application

BURNRATE's native desktop window (`myBurnRate`) renders a compact, frameless Windows interface using Edge WebView2 (with an automatic Tk/ttk fallback when WebView2 is unavailable) and an offline-embedded **Manrope** variable font. Light, Dark, and System themes, a split hero with an interactive interval timeline, a tactile segmented period bar, an inline summary strip, and a two-column grid of provider tiles that expand full-width for window meters and token breakdowns keep ten or more tools scannable without scrolling. On Windows, drag the header to move the window, resize from any edge, and use the header's pin, refresh, settings, minimize, and close controls. Taskbar restore, system tray mode, and Alt+F4 work out of the box.

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

- Choose **Today**, **7 days**, **Month** (this calendar month), or **All**.
- The large number is the recorded usage's **API-equivalent value**. Subscription
  fees, balances, provider-reported charges, and quota percentages are excluded.
  Until the first report arrives the hero shows a loading placeholder, never `$0.00`.
- The chart shows value per interval as bars, with the running total as a line.
  Hover it, or focus it and use the arrow keys, to see the total through an
  interval and what that interval added. If the hub sends no timeline (an older
  hub), the chart says so rather than drawing a shape.
- The strip under the list shows the **burn rate** (average value per hour for
  Today, per day otherwise), the **cache hit** share of prompt tokens, and
  **value vs plans**: the API-equivalent value divided by your configured plan
  cost over the same span. Plans are account-wide, so that ratio appears only for
  **All computers**. Unknown figures stay a dash.
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

## Install and update

Releases carry `myBurnRate_<version>_x64-setup.exe`. It installs for the current user
only (no administrator prompt) into `%LOCALAPPDATA%\Programs\BURNRATE`, adds Start Menu and
Desktop shortcuts named **myBurnRate**, and registers its own entry (**myBurnRate**) in Windows' installed
apps, separate from the older Tauri app's. `/S` installs silently. Your data stays in
`%LOCALAPPDATA%\BURNRATE`, which installing and updating never write; uninstalling removes only
the program and shortcuts, and offers (unticked, interactive only) to delete that data too.

An installed copy (the installer leaves a `BURNRATE.installed` file beside `BURNRATE.exe`) can
update itself. About a minute after it starts, then daily, it reads
`https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/native-latest.json`;
**Settings -> General -> Check for updates** does it on demand. **Install & restart** downloads the
installer into `%LOCALAPPDATA%\BURNRATE\updates`, verifies it and runs it silently
(`/S /UPDATE`), and the new version opens. A portable or source copy only shows a link to the
release page. The feed's single Windows entry is `{url, signature, sha256, size}`; the app
accepts it only if:

- the URL is `https://github.com/kamol-nazarov/myBurnRate/releases/download/v<version>/myBurnRate_<version>_x64-setup.exe`
  (redirects may only go to GitHub's release-asset hosts, over https);
- the download is at most 300 MiB with exactly the feed's size and SHA-256;
- its minisign signature verifies against the update key built into the app, the same key as the
  Tauri updater's, and the signed `file:` field is exactly that installer name, so a signed Tauri
  installer or an older native build cannot be substituted;
- the version is strictly newer than the running one (prereleases sort below their release);
- when the running `BURNRATE.exe` is Authenticode-signed, the installer is validly signed by the
  same publisher (a secondary check: the minisign signature is the primary one).

Nothing is run unless every check passed, and a file that fails one is deleted. Set
`BURNRATE_NO_UPDATE_CHECK=1` to turn all update checks off; see the
[privacy notes](privacy.md#update-checks-native-app) for exactly what is sent.

## Build the portable app and installer

```powershell
python -m pip install pyinstaller
python packaging/build_native.py                      # portable zip, plus the installer if NSIS is installed
python packaging/build_native.py --require-installer  # fail instead of skipping the installer
```

The installer is `packaging/build/myBurnRate_<version>_x64-setup.exe`, built with
[NSIS](https://nsis.sourceforge.io/) (`makensis` on `PATH`, or `BURNRATE_MAKENSIS`). Both
`BURNRATE.exe` and the installer are Authenticode-signed when signing is configured; see
[code signing](code-signing.md#signing-a-local-native-build). Otherwise the build says it is unsigned.

The windowless executable is `packaging/build/native/BURNRATE/BURNRATE.exe` on
Windows. Keep its `_internal` folder beside it. The portable archive is
`packaging/build/BURNRATE-native.zip`. It contains the native interface and the
existing collector in one bundle; `BURNRATE.exe --service` runs its collector.
The frozen application is tested with `BURNRATE.exe --demo --smoke-test`, which
renders sample data and closes without reading provider sources.
`python scripts/smoke-native.py` additionally starts the bundled collector in an
isolated temporary home, verifies process ownership and real desktop IPC, and
cleans up only that test process.

The native build has its own CI job, which builds the portable zip and the installer on
every push. A version tag's release workflow also publishes the native installer, its update
signature and `native-latest.json` beside the Tauri files (the Tauri updater's `latest.json` is
unchanged and ignores the native installer). The existing hub and spoke data protocol is
retained. Update the hub's code too to serve the new `/api/spend/usage` projection.
