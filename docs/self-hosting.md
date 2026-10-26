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
BURNRATE_DOMAIN=burnrate.example.com docker compose up -d
```

[`deploy/hub/compose.yaml`](../deploy/hub/compose.yaml) starts two containers:

- **`hub`** runs the image `ghcr.io/kamol-nazarov/myburnrate-hub`, built from
  the repository's [`Dockerfile`](../Dockerfile) by the release workflow for
  x86-64 and ARM. It keeps its database in the `hub-data` volume.
- **`caddy`** shares the hub's network, gets a Let's Encrypt certificate for
  your domain, and forwards to the hub.

Then, on your main computer, open setup and choose **Join my hub**, using
`https://burnrate.example.com` as the hub address. The first account created
on a hub is its owner. Further computers join with pairing codes made on the
hub; set `BURNRATE_ALLOW_REGISTRATION=1` only while adding another account.

- **Update:** `docker compose pull && docker compose up -d`. Pin a version
  with `BURNRATE_VERSION=0.4.0-beta.2`.
- **Back up:** copy `spend.db` out of the `hub-data` volume, for example with
  `docker compose cp hub:/data/spend.db ./spend-backup.db`.
- **Logs:** `docker compose logs -f hub`.

### On a tailnet instead of a public domain

Leave out the `caddy` service, publish nothing, and put Tailscale in front of
the hub instead. The simplest way is a `tailscale/tailscale` container that
shares the hub's network and runs
`tailscale serve --bg --https=443 http://127.0.0.1:17331`. Other computers on
your tailnet then use `https://<hub-machine>.<tailnet>.ts.net`.

## Without Docker

On a Linux server with Python 3.12:

```sh
git clone https://github.com/kamol-nazarov/myBurnRate.git /opt/burnrate
cd /opt/burnrate
python3.12 -m venv .venv && .venv/bin/pip install .
BURNRATE_CENTRAL_SERVER=1 .venv/bin/burnrate serve
```

Run that command as a systemd service, with `BURNRATE_CENTRAL_SERVER=1` in its
environment and `Restart=always`. Put Caddy, nginx or Tailscale Serve in front
of it for HTTPS.

`burnrate hub update --ssh <host>` updates a git-checkout hub from your own
computer: it fast-forwards the checkout, restarts the service and prints the
hub's build. Set its defaults with `BURNRATE_HUB_SSH`, `BURNRATE_HUB_PATH`,
`BURNRATE_HUB_SERVICE` and `BURNRATE_HUB_BRANCH`.
