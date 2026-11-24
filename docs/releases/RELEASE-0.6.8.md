# BURNRATE 0.6.8

- **A Claude account's limits follow the account, not one folder.** When the same Claude account is signed in at more than one place on this computer (for example `~/.claude-work` and a Traycer-managed profile), its lane shows the newest plan-usage reading from any of them instead of a days-old one from a folder that has gone idle.
- **Claude usage from another tool's profile lands on the right account.** Sessions that Traycer (or another tool) runs under a different Claude account are now counted on that account's lane instead of your default one, including a backfill of roughly the last 30 days.
