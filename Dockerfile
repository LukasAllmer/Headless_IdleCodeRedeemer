# Alpine keeps the runtime image small. Every dependency publishes musllinux
# wheels, so nothing is compiled from source and no build toolchain is needed.
#
# Deliberately buildable by the legacy builder as well as BuildKit -- no cache
# mounts, no syntax directive -- so it works on hosts without buildx installed.

FROM python:3.12-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer is cached until
# pyproject.toml or uv.lock actually change -- editing source does not rebuild it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
# --no-editable copies the package into site-packages instead of linking back to
# /app/src, so the runtime stage needs nothing but the venv.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-alpine AS runtime

LABEL org.opencontainers.image.title="Idle Code Redeemer" \
      org.opencontainers.image.description="Headless Idle Champions code redeemer"

# tzdata: Alpine ships no zoneinfo, and APScheduler resolves the local timezone
# at startup. Without it every run warns and silently assumes UTC.
RUN apk add --no-cache tzdata

# The named volume mounted at /data inherits this ownership, so the unprivileged
# user can create the database on first run.
RUN adduser -D -H -u 10001 icr \
    && mkdir -p /data \
    && chown icr:icr /data

COPY --from=builder --chown=icr:icr /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    ICR_DB_PATH=/data/icr.sqlite3 \
    ICR_WEB_HOST=0.0.0.0 \
    ICR_WEB_PORT=8787

WORKDIR /app
USER icr
VOLUME ["/data"]
EXPOSE 8787

# Logs go to stdout for `docker compose logs`; ICR_LOG_FILE is deliberately
# unset here rather than writing a rotating file inside the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0) if os.environ.get('ICR_WEB_ENABLED','true').lower() in ('0','false','no') else \
urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('ICR_WEB_PORT','8787') + '/', timeout=4)"

ENTRYPOINT ["icr"]
CMD ["serve"]
