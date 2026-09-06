FROM python:3.11-slim

# Install system dependencies, Node.js LTS and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user 'taktstock' with UID/GID 1000
RUN groupadd -g 1000 taktstock \
    && useradd -u 1000 -g taktstock -m -d /home/taktstock -s /bin/bash taktstock \
    && mkdir -p /run/taktstock-codex /run/taktstock-agy /run/taktstock-n8n /app \
    && chown -R taktstock:taktstock /run/taktstock-* /home/taktstock /app

# Generic and secure system-level Git configuration for container environments
RUN git config --system --add safe.directory '*' \
    && git config --system user.name 'Taktstock Bot' \
    && git config --system user.email 'bot@taktstock.local'

USER taktstock
ENV HOME=/home/taktstock

WORKDIR /app/server
CMD ["python3", "health_server.py"]
