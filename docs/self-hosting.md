# Running your own hub

A hub is the BURNRATE server your other computers sync to. You run it
yourself; there is no BURNRATE cloud. Any always-on machine works: a small VPS,
a home server or a desktop that stays on.

Like every BURNRATE install, the hub process listens on `127.0.0.1` only.
Something in front of it gives the other computers HTTPS: Caddy in the Docker
setup below, or [Tailscale Serve](tailscale.md).

## Docker (recommended)

You need Docker with Compose, and a domain name whose DNS points at the server
with ports 80 and 443 open.

```sh
git clone https://github.com/kamol-nazarov/myBurnRate.git
cd myBurnRate/deploy/hub
export BURNRATE_DOMAIN=burnrate.example.com
export BURNRATE_ACCESS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BURNRATE_SETUP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
docker compose up -d
```

[`deploy/hub/compose.yaml`](../deploy/hub/compose.yaml) starts two containers:

- **`hub`** runs the image `ghcr.io/kamol-nazarov/myburnrate-hub`, built from
  the repository's [`Dockerfile`](../Dockerfile) by the release workflow for
  x86-64 and ARM. It keeps its database in the `hub-data` volume.
- **`caddy`** shares the hub's network, gets a Let's Encrypt certificate for
  your domain, and forwards to the hub.

The hub refuses anonymous API access. Set a strong `BURNRATE_ACCESS_TOKEN` for bootstrap/recovery,
`BURNRATE_ALLOWED_HOSTS` to the public domain, and `BURNRATE_SETUP_TOKEN` for the first account.
Use HTTPS or a protected loopback tunnel; do not put credentials in URLs or shell history.

1. Open `/auth`, expand **Recovery and automation**, and create a recovery session with the
   break-glass credential. The browser receives an expiring HttpOnly session cookie.
2. At `/auth`, expand **Create the first owner account** and use the setup token. Existing owners simply sign in at `/auth`;
   the setup token is never needed again. Remove it from the environment afterwards.
3. Create a pairing code for each computer. It expires in 15 minutes and can be redeemed once.
   For a revoked/expired computer, create a fresh code with its machine ID or existing name.
4. On a paired computer, open `/auth` and sign in once for **All computers**. Pairing alone shows
   its own recorded usage. Owner sessions expire in 30 days; sync tokens expire in 90 days.
5. Set `BURNRATE_ALLOW_ADMIN_BEARER=0` when automation no longer needs the static bearer.
   `/auth` can revoke device sessions and rotate credentials. Credential rotation stores only the
   replacement hash in SQLite; save the returned credential securely. Updating only the old
   environment value does not override that hash.

- **Update:** the reference compose file pins both images by digest. Set
  `BURNRATE_HUB_IMAGE` to the new `tag@sha256:...` from the release
  (`docker buildx imagetools inspect ghcr.io/kamol-nazarov/myburnrate-hub:0.5.6`
  prints it), then `docker compose pull && docker compose up -d`.
- **Back up:** copy `spend.db` out of the `hub-data` volume, for example with
  `docker compose cp hub:/data/spend.db ./spend-backup.db`.
- **Logs:** `docker compose logs -f hub`.

### On a tailnet instead of a public domain

Leave out the `caddy` service, publish nothing, and put Tailscale in front of
the hub instead. The simplest way is a `tailscale/tailscale` container that
shares the hub's network and runs
`tailscale serve --bg --https=443 http://127.0.0.1:17331`. Other computers on
your tailnet then use `https://<hub-machine>.<tailnet>.ts.net`.

Add the tailnet hostname to `BURNRATE_ALLOWED_HOSTS` and set
`BURNRATE_ACCESS_TOKEN` exactly as for a public domain. A tailnet is a network
boundary, not an application authentication boundary: every device on it can
still reach the hub API.

## Without Docker

On a Linux server with Python 3.12:

```sh
git clone https://github.com/kamol-nazarov/myBurnRate.git /opt/burnrate
cd /opt/burnrate
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-build.txt
.venv/bin/pip install --no-deps .
BURNRATE_CENTRAL_SERVER=1 \
BURNRATE_ACCESS_TOKEN='<long-random-value>' \
BURNRATE_SETUP_TOKEN='<one-time-setup-value>' \
BURNRATE_ALLOWED_HOSTS='burnrate.example.com' \
BURNRATE_ALLOWED_ORIGINS='https://burnrate.example.com' \
.venv/bin/burnrate serve
```

Run that command as a systemd service, with `BURNRATE_CENTRAL_SERVER=1` in its
environment and `Restart=always`. Put Caddy, nginx or Tailscale Serve in front
of it for HTTPS.

`burnrate hub update --ssh <host>` updates a git-checkout hub from your own
computer: it requires a clean checkout, fast-forwards it, installs its Python
dependencies, checks the installed trust bundle, and runs an installed host
security gate before restarting the service and printing the hub's build.
Keep `BURNRATE_SUMMARY_PREWARM=0` on the central hub so background ingestion
does not compete with bounded interactive reads. Source, trust failures, or
unreviewed host drift stop the update before restart. Make a verified database
backup before an authorized update; never overwrite local changes to bypass
the clean-checkout gate. Set its defaults with `BURNRATE_HUB_SSH`, `BURNRATE_HUB_PATH`,
`BURNRATE_HUB_SERVICE` and `BURNRATE_HUB_BRANCH`.

## Auth model upgrade and rollback

Before an authorized upgrade, stop the hub and make a consistent offline backup of the SQLite
file and its configuration (or use SQLite's backup API while running). Retain the pre-upgrade
application revision. Never copy only a live WAL database file and assume it is a complete backup.
No upgrade is performed by this change or its tests.

On first start the additive auth tables and legacy metadata migration commit atomically. A second
start does not change migrated metadata or add a second migration audit row. Existing machine
hashes keep working for pushes until `created_at + 180 days`; old tokens are flagged for rotation.
Expired older tokens must be re-paired. Existing account passwords still work and need no setup
token. Old pairing codes carrying machine tokens are refused by the new redemption flow; an
already-extracted legacy machine token is subject to the migrated expiry and new read scope.

Run synthetic checks before rollout, then check anonymous reads return 401, own-machine reads
work, aggregate machine reads return 403 and an owner session sees all expected computers.
Review the newest 500 audit rows at `GET /api/auth/audit` using an owner session; archive the
append-only database through your backup policy. There is no automatic retention deletion.

Rollback requires stopping the upgraded hub, preserving its database as evidence, restoring the
complete pre-upgrade database/configuration snapshot, and running the previous application.
Do not rely on old code to enforce new expiry or read isolation merely because it ignores additive
tables. Sessions and rotations issued after the snapshot are lost. A restored snapshot may revive
old machine/break-glass credentials: rotate the recovered break-glass credential and re-pair
machines before network access. Rolling back restores the old single-owner security limitations;
prefer a forward fix. No destructive down-migration is provided.
