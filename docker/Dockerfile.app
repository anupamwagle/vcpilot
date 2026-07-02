FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY tests/ ./tests/
COPY pytest.ini ./pytest.ini
COPY .env.example .env.example

# Runs as root: app code is bind-mounted from the host at runtime (see
# docker-compose.yml), so a non-root uid would need to match the host's file
# ownership, which varies by machine (breaks on NAS setups where the repo is
# owned by a different uid/gid). Not a multi-tenant host, so no isolation lost.
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "scripts.init_db"]
