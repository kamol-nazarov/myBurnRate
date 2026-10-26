# BURNRATE hub: the server other computers sync to (see docs/self-hosting.md).
# The process binds 127.0.0.1 only, like every BURNRATE install; the Caddy
# container in deploy/hub/compose.yaml shares this container's network and
# terminates HTTPS in front of it.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BURNRATE_CENTRAL_SERVER=1 \
    BURNRATE_DATA_ROOT=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY spend_app ./spend_app
COPY spend_web ./spend_web
COPY pricing ./pricing
RUN pip install . \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin burnrate \
    && mkdir -p /data && chown burnrate /data

USER burnrate
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:17331/healthz', timeout=4)"

CMD ["burnrate", "serve", "--host", "127.0.0.1", "--port", "17331"]

LABEL org.opencontainers.image.title="BURNRATE hub" \
      org.opencontainers.image.description="Self-hosted hub that BURNRATE computers sync usage to" \
      org.opencontainers.image.source="https://github.com/kamol-nazarov/myBurnRate" \
      org.opencontainers.image.licenses="Apache-2.0"
