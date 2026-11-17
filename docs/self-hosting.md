# Running your own hub

A hub is the BURNRATE server your other computers sync to. You run it
yourself; there is no BURNRATE cloud. Any always-on machine on your tailnet
works: a home server, a desktop that stays on, or a VPS reachable over Tailscale.

## Tailnet-only requirement

**A BURNRATE hub must be tailnet-only (or private-network-only).** Never expose a
hub to the public Internet:

1. **First-account registration has no setup token.** Registration is open until
   the first owner account is created, then closes permanently. Anyone who can
   reach an uninitialized hub can claim that owner account.
2. **Release builds embed the hub address.** Packaged desktop builds ship the
   configured hub URL in `spend_app/_build_config.py`, so the hub's address
   itself is not a secret.

**Create the owner account right after deploying** (open the desktop app and sign
up, or post to `/api/account/register` over your tailnet). Do not leave a newly deployed hub
running without an owner account.

## Startup bind guard and reverse proxy headers

- **Bind exposure guard:** When `BURNRATE_CENTRAL_SERVER=1`, the hub refuses to
  start if bound to a public, non-private, non-tailnet, or wildcard interface
  (such as `0.0.0.0`, `::`, or a public IP). Allowed bind targets are loopback
  (`127.0.0.1`, `::1`, `localhost`), RFC 1918 private addresses, Tailscale CGNAT
  (`100.64.0.0/10`), and Tailscale IPv6 (`fc00::/7` / `fd7a:115c:a1e0::/48`).
  Set `BURNRATE_HUB_ALLOW_PUBLIC=1` only if you deliberately accept public-bind
  risk behind an external firewall.
- **Keep the app on `127.0.0.1:17331` behind Caddy or Tailscale Serve:** Put
  HTTPS termination in front of `127.0.0.1:17331` on the tailnet (for example,
  Caddy listening on your tailnet IP / `:8449` or `:443`, or
  [Tailscale Serve](tailscale.md)).
- **Trusted proxy headers (`BURNRATE_TRUSTED_PROXIES`):** `X-Forwarded-For` and
  `X-Forwarded-Proto` are honored only from configured proxy addresses (default:
  `127.0.0.1,::1`). Login and registration throttling keys off the real client
  IP forwarded by Caddy so computers on your tailnet do not share a single
  `127.0.0.1` throttle bucket, while spoofed headers from untrusted peers are
  ignored. Wildcards (`*`, `0.0.0.0/0`, `::/0`) are refused.

## Docker (tailnet-only, recommended)

You need Docker with Compose, Tailscale on the host, and a tailnet HTTPS
certificate (for example via Tailscale HTTPS / Caddy DNS-01) so ports are
published **only** on your Tailscale IP (`BURNRATE_TAILNET_IP`):

```sh
git clone https://github.com/kamol-nazarov/myBurnRate.git
cd myBurnRate/deploy/hub
export BURNRATE_TAILNET_IP=100.x.y.z
export BURNRATE_DOMAIN=burnrate.<tailnet>.ts.net
export BURNRATE_ACCESS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d
```

[`deploy/hub/compose.yaml`](../deploy/hub/compose.yaml) starts two containers:

- **`hub`** runs the image `ghcr.io/kamol-nazarov/myburnrate-hub`, built from
  the repository's [`Dockerfile`](../Dockerfile) by the release workflow for
  x86-64 and ARM. It binds `127.0.0.1:17331`, keeps its database in `hub-data`,
  and trusts `X-Forwarded-For` from Caddy on loopback (`BURNRATE_TRUSTED_PROXIES=127.0.0.1,::1`).
- **`caddy`** shares the hub's network and publishes HTTPS only on
  `${BURNRATE_TAILNET_IP}:443` (never `0.0.0.0`).

The hub refuses anonymous API access. Set a strong `BURNRATE_ACCESS_TOKEN` for
bootstrap/recovery and `BURNRATE_ALLOWED_HOSTS` to the tailnet hostname. Use
HTTPS over your tailnet; do not put credentials in URLs or shell history.

### First-run setup right after deploy

1. **Create the owner account immediately** from the desktop app (or via
   `POST /api/account/register`). No setup token is needed (`BURNRATE_SETUP_TOKEN` and
   `BURNRATE_ALLOW_REGISTRATION` are deprecated and ignored). Once the first
   account exists, registration closes permanently.
2. **Connect other computers:** Sign in with your owner email and password in
   the desktop app on each computer; owner sessions last 30 days and machine
   sync tokens last 90 days with automatic rotation.
3. **Recovery and automation:** Use `POST /api/auth/bootstrap` with `BURNRATE_ACCESS_TOKEN`
   to create a recovery session, or `POST /api/auth/rotate` on the hub to rotate credentials.
   The static bearer is restricted to bootstrap and rotation by default; set
   `BURNRATE_ALLOW_ADMIN_BEARER=1` only while automation still needs it for
   other API calls. Credential rotation happens on the hub and stores only the
   replacement hash in SQLite; updating the environment variable alone does not
   override it.

- **Update:** the reference compose file pins both images by digest. Set
  `BURNRATE_HUB_IMAGE` to the new `tag@sha256:...` from the release
  (`docker buildx imagetools inspect ghcr.io/kamol-nazarov/myburnrate-hub:0.5.7`
  prints it), then `docker compose pull && docker compose up -d`.
- **Back up:** copy `spend.db` out of the `hub-data` volume, for example with
  `docker compose cp hub:/data/spend.db ./spend-backup.db`.
- **Logs:** `docker compose logs -f hub`.

### With Tailscale Serve instead of Caddy

Leave out the `caddy` service, publish nothing, and run a `tailscale/tailscale`
container (or host `tailscale serve`) pointing at `127.0.0.1:17331`:
`tailscale serve --bg --https=443 http://127.0.0.1:17331`. Other computers on
your tailnet then use `https://<hub-machine>.<tailnet>.ts.net`.

Add the tailnet hostname to `BURNRATE_ALLOWED_HOSTS` and set
`BURNRATE_ACCESS_TOKEN` as above. A tailnet is a network boundary, not an
application authentication boundary: every device on it can still reach the hub
API, so always create your owner account right after deploying.

## Without Docker (Caddy on the tailnet)

On a Linux server with Python 3.12 and Tailscale:

```sh
git clone https://github.com/kamol-nazarov/myBurnRate.git /opt/burnrate
cd /opt/burnrate
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-build.txt
.venv/bin/pip install --no-deps .
BURNRATE_CENTRAL_SERVER=1 \
BURNRATE_ACCESS_TOKEN='<long-random-value>' \
BURNRATE_ALLOWED_HOSTS='<hub-machine>.<tailnet>.ts.net' \
BURNRATE_ALLOWED_ORIGINS='https://<hub-machine>.<tailnet>.ts.net:8449' \
BURNRATE_TRUSTED_PROXIES='127.0.0.1,::1' \
.venv/bin/python -m spend_app serve --host 127.0.0.1 --port 17331
```

Run that command as a systemd service with `BURNRATE_CENTRAL_SERVER=1` and
`Restart=always`, then front `127.0.0.1:17331` with Caddy bound to your
tailnet interface (for example `:8449` or `:443` on the Tailscale IP) or
Tailscale Serve. Immediately after starting the service, create the owner
account.

`burnrate hub update --ssh <host>` updates a git-checkout hub from your own
computer: it requires a clean checkout, fast-forwards it, installs its Python
dependencies, checks the installed trust bundle, and runs an installed host
security gate before restarting the service. It checks the public `/healthz`
liveness endpoint and reads the installed build over SSH, so the post-restart
check needs no API credential; the detailed `?supervisor=1` response is
owner-only on a hub. Keep `BURNRATE_SUMMARY_PREWARM=0` on the central hub so
background ingestion does not compete with bounded interactive reads. Source,
trust failures, or unreviewed host drift stop the update before restart. Make a
verified database backup before an authorized update; never overwrite local
changes to bypass the clean-checkout gate. Set its defaults with
`BURNRATE_HUB_SSH`, `BURNRATE_HUB_PATH`, `BURNRATE_HUB_SERVICE` and
`BURNRATE_HUB_BRANCH`.

## Auth model upgrade and rollback

Before an authorized upgrade, stop the hub and make a consistent offline backup of the SQLite
file and its configuration (or use SQLite's backup API while running). Retain the pre-upgrade
application revision. Never copy only a live WAL database file and assume it is a complete backup.
No upgrade is performed by this change or its tests.

On first start the additive auth tables and legacy metadata migration commit atomically. A second
start does not change migrated metadata or add a second migration audit row. Existing machine
hashes keep working for pushes until `created_at + 180 days`; old tokens are flagged for rotation.
Expired older tokens must be re-connected from the desktop app. Existing account passwords still
work and need no setup token; already-paired computers keep their machine tokens and keep syncing,
and the unused `pairing_grants` table is dropped on upgrade.

Run synthetic checks before rollout, then check anonymous reads return 401, own-machine reads
work, aggregate machine reads return 403 and an owner session sees all expected computers.
Review the newest 500 audit rows at `GET /api/auth/audit` using an owner session; archive the
append-only database through your backup policy. There is no automatic retention deletion.

Rollback requires stopping the upgraded hub, preserving its database as evidence, restoring the
complete pre-upgrade database/configuration snapshot, and running the previous application.
Do not rely on old code to enforce new expiry or read isolation merely because it ignores additive
tables. Sessions and rotations issued after the snapshot are lost. A restored snapshot may revive
old machine/break-glass credentials: rotate the recovered break-glass credential and reconnect
machines before network access. Rolling back restores the old single-owner security limitations;
prefer a forward fix. No destructive down-migration is provided.
