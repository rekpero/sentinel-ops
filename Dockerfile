FROM python:3.10-slim

WORKDIR /app

# System deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g bun && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Frontend build
COPY frontend/ frontend/
RUN cd frontend && bun install && bun run build

# Backend source
COPY backend/ backend/

# Config
COPY .env.example .env.example
COPY run.sh run.sh
RUN chmod +x run.sh

# Writable dirs
RUN mkdir -p data workspace logs

EXPOSE 8500

ENV SERVER_HOST=0.0.0.0
ENV SERVER_PORT=8500

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8500"]
