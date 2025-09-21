FROM python:3.11-slim

WORKDIR /app

# deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy code and static GTFS (folder)
COPY app.py .
COPY gtfs/ /app/gtfs/

# tell the app where GTFS lives (matches app default, but explicit is nice)
ENV GTFS_DIR=/app/gtfs
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
