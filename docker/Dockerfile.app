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

# Create non-root user
RUN useradd -m -u 1000 vcpilot && chown -R vcpilot:vcpilot /app
USER vcpilot

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "scripts.init_db"]
