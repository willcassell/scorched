FROM python:3.11-slim

WORKDIR /app

# Install system deps (needed for asyncpg compilation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

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

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
