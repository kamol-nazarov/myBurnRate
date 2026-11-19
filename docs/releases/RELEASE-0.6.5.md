# BURNRATE 0.6.5

- **Fix multi-computer sync deadlock after hub upgrades.** Spokes whose saved legacy sync hint fell behind the hub's acknowledged `usage_changes` sequence now rebase cleanly and verify overlapping history against hub storage instead of looping without delivering new usage.
- **Fix Windows Install & restart for managed Python desktops.** Launches `wheel_update.ps1` with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` so Windows PowerShell 5.1 actually executes the updater worker, accepts `uv`-managed Python junction paths when checking the running collector, and reports worker startup errors clearly.
