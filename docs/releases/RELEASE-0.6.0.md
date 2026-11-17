# BURNRATE 0.6.0

A small always-on-top tab and a rebuilt full window, both built around one question: is anything
about to run out? The tab answers it at a glance, and the full window now puts your limits first,
with a refill timeline for every window. There is no hub or protocol change.

## The tab

- **A strip that stays on top.** **Shrink to tab** in the full window (or **Show tab** in the tray
  menu) swaps the window for a small frameless strip with no taskbar button. It replaces the old
  Mini mode. Click a provider, the open button, or **Open BURNRATE** in a hover card to get the full
  window back.
- **The limit that runs out first, for each provider.** Every segment shows the window that will dry
  up soonest at your current pace: how much is left, a fuel bar, and `dry in ~1h` or `resets in 5d`.
  A balance shows its dollar amount instead.
- **Colour tells you how urgent it is.** Red means about to run out: dry within about 2 hours at
  your pace, 10% or less left, or a balance under $1. Amber means it will run dry before it
  refills, or a balance is under $5. The badge on the BURNRATE icon counts the providers about to
  run out.
- **Real logos and profile names.** Profiles show as `Primary · 5-hour` or `Work · weekly`.
- **Hover for the details.** The card lists every window with its % left, % used, reset time and a
  pace line, and the rest of the screen stays clickable around it.
- **It fits your monitor.** The tab shows up to five providers and folds calm ones into **+N fine**.
  Providers that need attention are never folded to make room; only when the screen is too narrow
  do the last of them fold into **+N to watch** (amber) or **+N urgent** (red). On a small or
  highly scaled monitor it also shortens the error chip to its code, drops secondary labels and
  abbreviates big balances (`$2.0K`). The exact amount stays in the tooltip and
  the card.
- **Drag it from anywhere.** Press and move to drag; a plain click still opens the full window. The
  tab remembers where you put it and stays on screen.
- **It fades like the full window.** With **Transparent when not hovered** on, the tab fades to
  your idle opacity about 1.5 seconds after the pointer leaves. It stays solid while the pointer,
  its card or keyboard focus is on it.
- **It says so when something is wrong.** While loading you see "Loading…", not placeholder values.
  With nothing to show it reads "No limits yet · Open BURNRATE to add a source". After a failed
  refresh the last values are dimmed and a chip shows the message and its code, for example "Can't
  reach your hub · NET-001". A provider with no reading yet says "Awaiting reading".
- **Keyboard friendly.** Tab through the segments, open the card with focus, and close it with
  **Esc**. Focus stays where it was when the tab refreshes.
- **BURNRATE remembers what you left open.** If the tab was up when you quit, it comes back as the
  tab. Starting BURNRATE a second time opens the full window of the copy that is already running.
  Switching is safe: if the tab can't be shown, the full window stays.

## The full window

- **Bigger, limits first.** The window opens at 440 × 640 (it was 380 × 556) and can shrink to
  380 × 520. Limits come first, most urgent first, tagged **About to run out**, **Dry before
  refill** or **Out of quota**.
- **A refill timeline for every window.** It shows how long you can keep going at your pace, where
  you run dry before the refill, and when it refills, with a note like "Dry in ~1h · refills in
  2h". A window with no pace forecast yet shows a soft bar of what is left ("Refills in 3h"), and an
  exhausted one reads "Out · refills in 2h".
- **Everything else is still here,** restyled for the new size: Today, Running now, cost by model
  with its drilldown, token mix, the activity heatmap and your plans, now with prices such as
  `$200/mo`.
- **The footer holds the window actions:** Diagnostics, Share week, **Keep on top** (moved out of
  the title bar), density and Settings.
- **Clearer states.** Before any source has reported you see "Waiting for the first readings" and an
  **Add a source** link. If a refresh fails you get a banner with the plain message and its code,
  and the last values stay on screen marked as stale.

## Behind the scenes

- **One urgency rule for both windows.** Pace, urgency and order are worked out once, in
  `spend_app/provider_urgency.py`, so the tab and the full window always agree. Profiles of one
  provider sit together only when they are equally urgent.
- **A desktop log.** The app writes `%LOCALAPPDATA%\BURNRATE\logs\desktop.log` (rotating, with
  secrets redacted) so a window problem leaves a trace.

## Upgrading from 0.5.9

1. **Nothing to do on the hub.** The protocol version is still 2 and no hub code changed, so a
   0.6.0 desktop works with a 0.5.9 hub.
2. **The window opens at the new size** unless you saved one. A saved size is kept, though anything
   below the new 380 × 520 minimum is raised to it.
3. **If you left the app in Mini mode,** it now opens as the tab.
4. **A saved provider order still wins** in the full window, so you may not see most urgent first
   right away. Dragging providers sets your own order; to go back, open Settings → Custom provider
   order and choose **Reset**. The tab always sorts by urgency.

## Removed

- **The in-page Mini mode,** with its mini bar and the Full/Mini toggle. The tab replaces it.

See the [changelog](../../CHANGELOG.md) for file and setting names.
