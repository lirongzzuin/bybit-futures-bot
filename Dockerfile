FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl tzdata sqlite3 && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Seoul
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── Create app user (non-root for security) ───────────────────
RUN useradd -m -s /bin/bash botuser
WORKDIR /app

# ── Python deps (cached layer — only reinstalls on requirements change) ───────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── App code ──────────────────────────────────────────────────
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# ── Runtime dirs ──────────────────────────────────────────────
RUN mkdir -p logs reports data_cache && \
    chown -R botuser:botuser /app

USER botuser

# ── Health check ──────────────────────────────────────────────
# Verifies the config loads (catches bad .env at startup)
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "from src.config import settings; print(settings.TRADING_MODE)" || exit 1

# ── Entry point ───────────────────────────────────────────────
CMD ["python", "-m", "src.main"]
