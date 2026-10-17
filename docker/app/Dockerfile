# CopperChat app image — build context: repository root.
#   docker build -f docker/app/Dockerfile \
#     --build-arg KICAD_COMMIT=$(cat docker/kicad-base/KICAD_COMMIT) -t copperchat:dev .
# Layers: KiCad base image (KiCad 11 from source, user copperchat uid 1000)
#         + Python backend (uv, /app/backend/.venv) + built frontend (/app/frontend/dist).
ARG KICAD_COMMIT

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# --include=dev: the build tools are devDependencies (a NODE_ENV=production env would drop them).
RUN npm ci --include=dev --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM ghcr.io/peterus/copperchat-kicad-base:${KICAD_COMMIT}
ARG KICAD_COMMIT
LABEL org.opencontainers.image.source="https://github.com/peterus/copperchat" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.title="CopperChat" \
      org.opencontainers.image.description="Chat with an AI agent about your KiCad projects. Powered by Claude." \
      io.copperchat.kicad-commit="${KICAD_COMMIT}"
# X11: the base image ends with USER copperchat (uid 1000); install as root, run as copperchat.
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/backend/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PROJECTS_ROOT=/projects \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/frontend/dist \
    HOST=0.0.0.0 \
    PORT=8080
WORKDIR /app/backend
# Dependencies first (cached unless the lock changes); kicad-python comes from its pinned git revision.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version backend/README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --locked --no-dev --no-editable \
 && python -c "import copperchat.web.app, kipy.schematic, claude_agent_sdk, pywebpush"
COPY --from=frontend /src/frontend/dist /app/frontend/dist
RUN mkdir -p /data /projects \
 && chown -R copperchat:copperchat /app /data /projects
USER copperchat
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT', '8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "copperchat"]
