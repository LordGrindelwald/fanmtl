# Use Python 3.9 slim (matches previously working setup)
FROM python:3.9-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Install system dependencies
# Includes build tools needed temporarily, Python package dependencies, and Chromium
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Build tools (will be removed later)
    build-essential \
    # Python package dependencies
    wget ca-certificates gnupg \
    libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev libjpeg62-turbo-dev \
    # --- Install Chromium and Driver via apt ---
    chromium \
    chromium-driver \
    # --- Chromium runtime dependencies ---
    fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 \
    libcups2 libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 libx11-6 \
    libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 libxdamage1 libxext6 \
    libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
    lsb-release xdg-utils \
    # --- CLEANUP ---
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container.
WORKDIR /app

# Copy the requirements file (ensure it has selenium==3.141.0)
COPY requirements.txt .
# Install the Python dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Add the /app directory to Python's import path
ENV PYTHONPATH=/app

# Copy the application code
# This will also copy your new .py and .sh files
COPY . .

# FIX: Move 'sources' inside 'lncrawl' and create __init__.py files
RUN if [ -d /app/sources ] && [ -d /app/lncrawl ]; then \
        mv /app/sources /app/lncrawl/sources && \
        find /app/lncrawl/sources -type d -exec touch {}/__init__.py \; ; \
    else \
        echo "Warning: /app/sources or /app/lncrawl directory not found." >&2; \
    fi

# Make the new start script executable
RUN chmod +x /app/start.sh

# Expose the port
EXPOSE 8080

# The command to run your web service and bot
CMD ["/app/start.sh"]
