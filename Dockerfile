# Dockerfile - Render-compatible, installs Playwright dependencies and Chromium at build time
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install apt dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    libxkbcommon0 \
    libgbm1 \
    wget \
    gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy app and requirements
COPY requirements.txt /app/requirements.txt
COPY app.py /app/app.py

# Install Python dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers at build time (preferred)
RUN python -m playwright install chromium

# Expose port (Render sets $PORT automatically)
ENV PORT 5000
EXPOSE 5000

# Use gunicorn in container; allow $PORT expansion
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 120"]
