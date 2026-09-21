# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ffmpeg does the audio extraction and chunking; tini reaps the uvicorn children
# so `docker stop` is clean.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Dependencies first so code edits do not bust the wheel cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Unraid bind-mounts this; declaring it keeps clips out of the container layer.
VOLUME ["/data"]
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8099/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--proxy-headers"]
