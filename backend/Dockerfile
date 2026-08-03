# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
FROM python:3.12-slim AS deps

WORKDIR /app
ARG CLAWITH_APT_MIRROR=deb.debian.org
RUN if ! python -c 'import re, sys; host = sys.argv[1]; label_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"); raise SystemExit(0 if 0 < len(host) <= 253 and all(label_pattern.fullmatch(label) for label in host.split(".")) else 1)' "$CLAWITH_APT_MIRROR"; then \
        echo "invalid CLAWITH_APT_MIRROR host" >&2; exit 1; \
    fi && \
    if [ "$CLAWITH_APT_MIRROR" != "deb.debian.org" ]; then \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|$CLAWITH_APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
        else \
            sed -i "s|deb.debian.org|$CLAWITH_APT_MIRROR|g" /etc/apt/sources.list; \
        fi; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info pango1.0-tools && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
ARG CLAWITH_PIP_INDEX_URL
ARG CLAWITH_PIP_TRUSTED_HOST
RUN if [ -n "$CLAWITH_PIP_INDEX_URL" ] && [ -n "$CLAWITH_PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" --trusted-host "$CLAWITH_PIP_TRUSTED_HOST" "uv==0.11.26" && \
        uv sync --frozen --no-dev --no-install-project --no-cache \
            --default-index "$CLAWITH_PIP_INDEX_URL" --allow-insecure-host "$CLAWITH_PIP_TRUSTED_HOST"; \
    elif [ -n "$CLAWITH_PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" "uv==0.11.26" && \
        uv sync --frozen --no-dev --no-install-project --no-cache \
            --default-index "$CLAWITH_PIP_INDEX_URL"; \
    else \
        pip install --no-cache-dir "uv==0.11.26" && \
        uv sync --frozen --no-dev --no-install-project --no-cache; \
    fi

# ─── Production ─────────────────────────────────────────
FROM python:3.12-slim AS production

WORKDIR /app
ARG CLAWITH_APT_MIRROR=deb.debian.org
RUN if ! python -c 'import re, sys; host = sys.argv[1]; label_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"); raise SystemExit(0 if 0 < len(host) <= 253 and all(label_pattern.fullmatch(label) for label in host.split(".")) else 1)' "$CLAWITH_APT_MIRROR"; then \
        echo "invalid CLAWITH_APT_MIRROR host" >&2; exit 1; \
    fi && \
    if [ "$CLAWITH_APT_MIRROR" != "deb.debian.org" ]; then \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|$CLAWITH_APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
        else \
            sed -i "s|deb.debian.org|$CLAWITH_APT_MIRROR|g" /etc/apt/sources.list; \
        fi; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl shadowsocks-libev gosu bubblewrap libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info pango1.0-tools fontconfig fonts-wqy-microhei fonts-wqy-zenhei fonts-noto-cjk chromium && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

# Keep the large browser/font runtime layer cacheable. ffmpeg changes independently
# and is needed by the media pipeline for deterministic video text overlays.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/
COPY --from=deps /app/.venv/ /app/.venv/

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Copy application code
COPY . .

RUN useradd --create-home clawith && \
    mkdir -p /data/agents && \
    chmod +x /app/entrypoint.sh && \
    chown -R clawith:clawith /app /data

# Note: USER is removed to allow entrypoint.sh to fix permissions of mounted volumes
# at runtime. The entrypoint script will drop privileges to 'clawith' after fixing permissions.

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
# entrypoint.sh bootstraps Alembic and LangGraph checkpoints before `uvicorn`
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
