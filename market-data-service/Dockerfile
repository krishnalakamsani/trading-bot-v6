FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# install backend deps first, then MDS-specific deps (adds asyncpg, fastapi, etc.)
COPY backend/app/requirements.txt ./requirements-backend.txt
COPY market-data-service/requirements.txt ./requirements-mds.txt
RUN pip install --no-cache-dir -r requirements-backend.txt -r requirements-mds.txt

# Copy backend app code first (shared modules)
COPY backend/app/ .

# Overlay MDS-specific files (these overwrite if same name)
COPY market-data-service/ .

RUN mkdir -p /app/data /app/logs

EXPOSE 8002

CMD ["python3", "/app/mds_service_runner.py"]
