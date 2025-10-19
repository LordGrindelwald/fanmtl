# Use a slim Python 3.9 base image for compatibility
FROM python:3.9-slim as builder

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Install build dependencies and Chromium in a single layer to reduce size
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg62-turbo-dev \
    chromium \
    chromium-driver \
    # Cleanup build dependencies and apt cache
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Use a new, clean stage for the final image to keep it small
FROM python:3.9-slim

# Copy only the necessary installed browser from the builder stage
COPY --from=builder /usr/lib/chromium /usr/lib/chromium
COPY --from=builder /usr/bin/chromium /usr/bin/chromium
COPY --from=builder /usr/bin/chromium-driver /usr/bin/chromium-driver
COPY --from=builder /usr/share/doc/chromium /usr/share/doc/chromium

# Install runtime dependencies for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    wget \
    xdg-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Add the /app directory to Python's import path
ENV PYTHONPATH=/app

# Copy the application code
COPY . .

# Move sources directory and create __init__.py files
RUN if [ -d /app/sources ] && [ -d /app/lncrawl ]; then \
        mv /app/sources /app/lncrawl/sources && \
        find /app/lncrawl/sources -type d -exec touch {}/__init__.py \; ; \
    else \
        echo "Warning: /app/sources or /app/lncrawl directory not found." >&2; \
    fi

# Expose the port
EXPOSE 8080

# The command to run your web service
CMD gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 telegram_bot:server_app
