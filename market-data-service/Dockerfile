FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Use backend's full requirements (MDS reuses dhan_api, config, database, etc.)
COPY backend/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend app code first (shared modules)
COPY backend/app/ .

# Overlay MDS-specific files (these overwrite if same name)
COPY market-data-service/ .

RUN mkdir -p /app/data /app/logs

EXPOSE 8002

CMD ["python3", "/app/mds_service_runner.py"]
