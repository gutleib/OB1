FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py db.py embeddings.py metadata.py schema.sql ./

# Non-root user
RUN adduser --system --no-create-home appuser
USER appuser

EXPOSE 7981

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:7981/health || exit 1

CMD ["python", "server.py"]
