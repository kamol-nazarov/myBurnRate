# BURNRATE 0.5.9

Account-only multi-computer sync: passwordless pairing codes and legacy pairing
CLI flags are removed, while computers already connected to a hub continue syncing
uninterrupted.

## Pairing removed

- **Account sign-in replaces pairing codes.** Computers connect to the hub by
  creating the first owner account or signing in with email and password.
  Removed `spend_app/pairing.py` (moving `hub_origin` to
  `spend_app/hub_origin.py`), `/api/pairing/redeem`, `/api/setup/pair-hub`,
  `/api/setup/connect-spoke`, `PAIRING_PREFIX`, `encode_pairing_code`,
  `decode_pairing_code`, `ensure_tailscale_hub_proxy`, `detect_network_addresses`,
  `PAIRING_MINUTES`, desktop pairing UI and bridge methods (`create_pairing_code`,
  `join_with_code`), and CLI `burnrate machines register`, `sync --hub --token`,
  and `desktop --hub --token`.
- **Log redaction preserved.** `spend_app/redact.py` continues to mask historical
  `brpair_` values when reading or exporting older logs.

## Upgrade

- **Existing machines keep syncing.** Computers already connected keep their
  machine bearer tokens and sync cursors without re-authenticating.
- **Database cleanup.** Startup migration drops the obsolete `pairing_grants`
  table idempotently, discarding any unused pairing codes.
