# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Sources + migrations (alembic.ini and migrations/ stay in /app: `alembic upgrade head` runs from there).
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf build src/*.egg-info

COPY alembic.ini ./
COPY migrations ./migrations
# Mini App build (may be absent: the backend then serves its smoke page). The package lives in site-packages,
# so the compose file points WEB_DIST_DIR here.
COPY web/dist ./web/dist

# Secrets are NOT baked into the image: .env is excluded by .dockerignore and injected by compose (env_file).
RUN useradd --system --uid 10001 --home-dir /app app && chown -R app /app
USER app

CMD ["python", "-m", "ai_civ.bot"]
