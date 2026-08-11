# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# tradabot API image.
#
# Multi-stage: dependencies are installed into a virtualenv in the builder and
# copied into a slim runtime, so build toolchains never ship in the final image.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed for any dependency without a manylinux wheel;
# libpq-dev for psycopg2 (used by Alembic's sync migration engine).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the dependency resolution needs, so the layer caches across
# source changes. README.md is required because pyproject references it.
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py

RUN pip install --upgrade pip \
    && pip install "psycopg2-binary>=2.9" \
    && pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 tradabot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=tradabot:tradabot app ./app
COPY --chown=tradabot:tradabot migrations ./migrations
COPY --chown=tradabot:tradabot alembic.ini pyproject.toml README.md ./
COPY --chown=tradabot:tradabot docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

# Never run as root.
USER tradabot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
