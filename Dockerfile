FROM python:3.12-slim

# Build args to match host user (avoids root-owned files on mounted volumes)
ARG UID=1000
ARG GID=1000

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user matching the host UID/GID
RUN groupadd -g ${GID} appuser 2>/dev/null || true && \
    useradd  -u ${UID} -g ${GID} -s /bin/bash -m appuser 2>/dev/null || true

WORKDIR /usr/src/app

# Install Python dependencies at build time (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-u", "python/bot.py"]