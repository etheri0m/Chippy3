FROM python:3.12-slim-bookworm

WORKDIR /app

# System deps for pigpio client library and serial access
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency file first for layer caching
COPY pyproject.toml uv.lock* ./

# Install uv and project dependencies
RUN pip install --no-cache-dir uv && \
    uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Copy application code
COPY *.py ./

# Dashboard port
EXPOSE 8080

# Default: run full system. Override with SKIP_HARDWARE=1 for no motors.
ENV SKIP_HARDWARE=0

CMD ["uv", "run", "main.py"]