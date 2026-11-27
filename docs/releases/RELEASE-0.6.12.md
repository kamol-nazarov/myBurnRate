# BURNRATE 0.6.12

- **Hub sync fix.** Raised the sync quota `label` text bound from 128 to 512 characters (and defensively capped exported quota labels) so BURNRATE's own unavailable-quota diagnostic messages sync cleanly instead of returning `422`.
- **Claude Work profile usage.** Included extra profile lanes (like `claude-code-work` and `opencode-go-2`) in Plans & Value matching, transitioned session-split Claude extra profiles out of `waiting_activity` in Sources, recognized extra profile lanes in Traycer deduplication, and wired `weekly-fable` window tokens without double-counting weekly totals.
- **Windows installer script.** Fixed `install-native.ps1` decoding of `native-latest.json` when GitHub returns raw bytes (`application/octet-stream`).
