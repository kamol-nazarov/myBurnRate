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

The hub refuses anonymous API access. `BURNRATE_ACCESS_TOKEN` is required,
`BURNRATE_ALLOWED_HOSTS` is set to the public domain, and the first account
also needs `BURNRATE_SETUP_TOKEN`. Keep both tokens private; anyone with the
access token can read every computer's usage.

Then:

1. Open `https://burnrate.example.com/?token=<BURNRATE_ACCESS_TOKEN>` once to
   set the admin cookie, and bookmark the URL without the token afterwards.
2. Open setup, choose **Join my hub**, and create the first account with the
   setup token. That account is the owner.
3. On each other computer, open setup and choose **Join my hub** with a pairing
   code made on the hub; set `BURNRATE_ALLOW_REGISTRATION=1` only while adding
   another account.
4. Remove `BURNRATE_SETUP_TOKEN` from the environment once the owner exists
   (or keep it; it is only consulted while the hub has no account).

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
