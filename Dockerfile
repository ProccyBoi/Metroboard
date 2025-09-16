FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# routes.txt and stops.txt must be in the image (same versions you use locally)
COPY stops.txt routes.txt trips.txt stop_times.txt agency.txt /app/
COPY app.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
