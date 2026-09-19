# Unified AI Job Application Platform
#
# Ships the API and the scheduler from one image; the command chooses which.
# Playwright's browsers and a LaTeX toolchain are both installed, because a
# deployment without them can neither discover jobs nor produce a PDF resume —
# the two things the platform exists to do.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System packages, including a minimal TeX Live. texlive-latex-recommended
# carries the packages resume_templates/styles/ats.sty needs (geometry,
# titlesec, enumitem, hyperref); the full distribution would add ~4 GB for
# nothing we use.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        lmodern \
        ca-certificates \
        curl \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Dependencies before source, so editing code does not reinstall the world.
COPY pyproject.toml ./
# The postgres extra pulls in psycopg, which docker-compose's DATABASE_URL
# needs. Harmless on SQLite.
RUN pip install --upgrade pip && pip install ".[postgres]"

# Playwright's own browser and its OS dependencies.
RUN playwright install --with-deps firefox

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY resume_templates ./resume_templates
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Runtime state lives on a volume; nothing here should write into the image.
RUN mkdir -p /app/data /app/.logs \
    && useradd --create-home --uid 10001 jobpilot \
    && chown -R jobpilot:jobpilot /app /ms-playwright
USER jobpilot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# Migrations run first, then whatever command is given.
ENTRYPOINT ["docker-entrypoint.sh"]

# API by default. Override with `python -m app.cli run` for the scheduler.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
