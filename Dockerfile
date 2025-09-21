# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Keep logs unbuffered & avoid pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---- Install deps ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- App code ----
COPY app.py .

# Ensure a place for GTFS to be extracted
RUN mkdir -p /app/gtfs
ENV GTFS_DIR=/app/gtfs

# Default GTFS ZIP (can be overridden by env on your platform)
ENV GTFS_STATIC_ZIP_URL=https://github.com/ProccyBoi/Metroboard/releases/download/v1.0-gtfs/gtfs.zip

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
