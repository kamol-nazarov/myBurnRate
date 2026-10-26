# BURNRATE v0.3.0-beta.16

The Sidecar HUD is now the product. The full dashboard is off by default and
returns with `BURNRATE_ENABLE_DASHBOARD=1`; `/` opens the HUD, and the desktop
app starts with its main window hidden (shown only for setup).

HUD numbers stay on screen while polling: a meter whose newest poll returns no
value keeps its last reading until its window resets. On a paired computer the
HUD shows the last hub copy at once and renews it in the background, so a slow
hub no longer stalls it.

OpenCode Go is tracked on its own: Go messages no longer count toward the Z.AI
Coding Plan, are valued from the official Go rates, and a new card shows the
5-hour, weekly and monthly Go limits.

Cursor is no longer collected or counted, because its usage duplicates SuperGrok
usage. Each database removes its recorded Cursor rows once, after a verified
backup in `backups/pre-cursor-removal-*.db`, and a hub ignores Cursor rows from
computers on an older version.

The Grok Build collection pass drops from about 15 seconds to about 3 while a
session is active.

Update the hub as well as each computer, then restart BURNRATE and reopen the
HUD. The broader suite still has the same 23 failures reproduced on the previous
commit; none are new.
