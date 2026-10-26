# BURNRATE v0.3.0-beta.15

The Sidecar HUD starts live limits immediately, without waiting for a full
history import or dashboard calculations. Recorded spend statistics load
separately from quota meters and active agents.

Opening and using the full dashboard requests historical collection. After
two minutes without dashboard requests, collection finishes its current
source and defers further scans. Live polling and spoke sync continue.

Live update streams now read SQLite and the hub outside the web server's event
loop, and use SQLite's change counter instead of repeated history scans.
Deferred history no longer triggers a collection-stalled watchdog restart or
a history warning in the HUD.

Reopen existing dashboard and HUD windows after updating to load the new
browser scripts. Update the hub as well as each computer for the new lightweight
HUD endpoints and consistent account quota display.

Validation includes the focused polling/API tests, required browser checks,
a clean package-install smoke test, and the Windows desktop release build.
The broader suite still has 22 failures reproduced on the unchanged previous
commit; those existing failures are outside this polling fix.
