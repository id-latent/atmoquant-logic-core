# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies for scipy/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="AQL NODE"
LABEL description="AtmoQuant Logic — Polymarket Temperature Arbitrage Engine"

# Non-root user for security
RUN useradd -m -u 1000 aql
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=aql:aql . .

# Create persistent data directory
RUN mkdir -p /app/data && chown aql:aql /app/data

USER aql

# ── Environment Defaults (overridden by Railway env vars) ─────────────────────
ENV PORT=8080 \
    LOG_LEVEL=INFO \
    BANKROLL_USD=200.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# Health check for Railway
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8080/health', timeout=5); exit(0 if r.status_code == 200 else 1)"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
