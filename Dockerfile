# BURNRATE hub: the server other computers sync to (see docs/self-hosting.md).
# The process binds 127.0.0.1 only, like every BURNRATE install; the Caddy
# container in deploy/hub/compose.yaml shares this container's network and
# terminates HTTPS in front of it.

FROM python:3.12-slim AS wheel

ARG BURNRATE_PUBLIC_BUILD=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE requirements-build.txt hatch_build.py ./
COPY packaging/build_config.py ./packaging/build_config.py
COPY spend_app ./spend_app
COPY pricing ./pricing
COPY assets/icons ./assets/icons
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements-build.txt \
    && BURNRATE_PUBLIC_BUILD="$BURNRATE_PUBLIC_BUILD" python -m build --wheel --no-isolation --outdir /wheel

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BURNRATE_CENTRAL_SERVER=1 \
    BURNRATE_SUMMARY_PREWARM=0 \
    BURNRATE_DATA_ROOT=/data

WORKDIR /app
COPY requirements-runtime.txt ./
RUN --mount=type=cache,target=/root/.cache/pip --mount=from=wheel,source=/wheel,target=/wheel,ro \
    PIP_NO_CACHE_DIR=0 pip install --require-hashes -r requirements-runtime.txt \
    && pip install --no-deps --no-build-isolation /wheel/*.whl \
    && pip check \
    && python -c "from importlib.metadata import version; assert tuple(map(int, version('certifi').split('.'))) >= (2026, 10, 23); from spend_app.bounded_reads import BoundedReadExecutor" \
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
