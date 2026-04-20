FROM python:3.11-slim

WORKDIR /app

# Install system deps. gcc + libpq-dev are needed for asyncpg. `git` lets the
# guidance panel surface analyst_guidance.md edit history — the .git directory
# is bind-mounted read-only via docker-compose.yml.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# The bind-mounted /app/.git is owned by the host user, not root. Modern git
# refuses to operate on such repos ("dubious ownership") unless safe.directory
# is set. Container is read-only-consumer, so whitelisting /app is fine.
RUN git config --global --add safe.directory /app

# Copy project metadata and source — setuptools needs src/ present to build
COPY pyproject.toml ./
COPY src/ ./src/

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Copy entrypoint
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV TZ=America/New_York

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
