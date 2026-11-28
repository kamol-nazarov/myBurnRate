# Hosting myBurnRate Cloud (`BURNRATE_CLOUD_SERVER=1`)

This guide deploys the multi-user **myBurnRate Cloud** backend cleanly in an isolated Docker Compose stack alongside existing workloads on a Linux server, and shows how to move it to a dedicated VPS later.

## Architecture at a Glance

- **Central auth database:** `/data/cloud/auth.db` (SQLite WAL) stores user accounts, pending 2-step registrations, hashed 6-digit OTPs, 8-character pairing codes, cloud sessions (`br_cs_*`), machine tokens (`br_cm_*`), and append-only `cloud_audit`.
- **Physically isolated tenant databases:** One SQLite file per verified user at `/data/cloud/users/<user_id>/spend.db`. No database file is created until email verification completes.
- **Desktop-app-only API:** There is no public web dashboard. Desktop apps connect over HTTPS (`api.myburnrate.io`) using Bearer credentials stored in the OS keychain.
- **Transactional email:** Resend HTTP API (`https://api.resend.com/emails`) sends 6-digit signup/reset/email-change codes and security alerts.

## 1. Prerequisites

1. Point DNS `A` / `AAAA` records for `api.myburnrate.io` (or your chosen subdomain) to your server.
2. Verify your sending domain in **Resend** (SPF + DKIM) and create a sending-only API key (`re_...`).
3. Docker Engine + Docker Compose v2 installed on the server.

## 2. Configure & Launch

```bash
cd deploy/cloud
cp .env.example .env
chmod 600 .env
# Edit .env: set BURNRATE_CLOUD_DOMAIN, BURNRATE_RESEND_API_KEY, BURNRATE_EMAIL_FROM
docker compose up -d --build
```

Verify health:

```bash
curl -sS https://api.myburnrate.io/api/hub/info
# {"service":"burnrate","protocolVersion":2,"mode":"cloud"}
```

> If your server already has Caddy or Nginx bound to ports `80`/`443`, set `BURNRATE_CLOUD_HTTP_BIND=127.0.0.1:8080` and `BURNRATE_CLOUD_HTTPS_BIND=127.0.0.1:8443` in `.env` (or proxy directly to the `cloud-hub` service on your existing reverse proxy).

## 3. Continuous Backups & Per-User Snapshots

- **Central `auth.db` streaming:** Fill in the `LITESTREAM_*` variables in `.env` (Cloudflare R2 or S3) and launch with:
  ```bash
  docker compose --profile backups up -d
  ```
- **Online snapshot of all tenant DBs (`users/*/spend.db`):** Schedule a daily cron job using the packaged online SQLite backup module:
  ```bash
  docker compose exec -T cloud-hub python -m spend_app.cloud.backup \
    --cloud-root /data/cloud --out /data/cloud/backups
  ```

## 4. Moving to a Dedicated VPS Later

Because all state lives under the single `cloud_data` volume (mounted at `/data`, with cloud databases under `/data/cloud`):

1. Stop `cloud-hub` briefly on the old server (`docker compose stop cloud-hub`).
2. Sync `/data/cloud` to the new VPS via `rsync -aHAX` (or restore `auth.db` via Litestream + latest tenant backup).
3. Start `docker compose up -d --build` on the new VPS and switch DNS. Zero app changes needed.
