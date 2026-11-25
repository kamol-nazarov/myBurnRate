# BURNRATE 0.6.11

- **Hub access.** Revoking a computer also invalidates the owner sessions bound to it. First-owner enrollment requires a setup credential, and sign-in throttling is scoped to the caller and account.
- **Synced data.** The hub validates and bounds batches, preserves the original reporter's authority over verified billing data, and stores tool labels in place of activity titles. Historical synced titles are normalized during the upgrade.
- **Desktop access and updates.** A per-install credential protects the local API. Desktop sign-in shows the configured hub and credentials stay bound to that hub. Public builds use your per-user hub setting. The release pipeline verifies signed version tags and pinned build inputs.
- **Upgrade order.** Back up the hub database, upgrade the hub, and verify sync from existing desktops before updating them.
