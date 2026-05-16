FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stops.txt routes.txt trips.txt agency.txt /app/
COPY gtfs/stop_times.txt.gz /app/gtfs/stop_times.txt.gz
COPY scripts/ensure_stop_times.py /app/scripts/ensure_stop_times.py
ENV STOP_TIMES_PATH=/app/gtfs/stop_times.txt
RUN python /app/scripts/ensure_stop_times.py
COPY station_line_pairs.txt station_line_pairs.csv /app/
COPY boards.json /app/
RUN mkdir -p /app/data
COPY data/boards.json /app/data/boards.json
ENV BOARD_SETTINGS_PATH=/app/data/boards.json
COPY firmware /app/firmware
COPY metroboard_public.ino /app/metroboard_public.ino
COPY app.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
