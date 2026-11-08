FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (build-essential for compiling C extensions if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    TIMETABLE_DEV_MODE=1 \
    MALLOC_ARENA_MAX=2 \
    CP_SAT_WORKERS=2 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
