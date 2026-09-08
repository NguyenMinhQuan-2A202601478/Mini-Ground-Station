# syntax=docker/dockerfile:1

# One image, three processes. The API, the worker, and the simulator share the
# same code and the same dependencies; giving each its own image would triple
# the build time to save nothing.

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- build ---------------------------------------------------------------
FROM base AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.9.7
WORKDIR /app

# Dependencies first, from the lockfile, so editing source does not re-resolve
# or re-download anything.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra ml

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra ml

# --- runtime -------------------------------------------------------------
FROM base AS runtime
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/migrations ./migrations
COPY --from=builder /app/alembic.ini ./

# Nothing here needs root, and a compromised ingestion endpoint should not get
# it. Migrations run as the same user; they only need the database.
RUN useradd --system --uid 10001 --no-create-home mgs && chown -R mgs:mgs /app
USER mgs

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["mgs-api"]
