# Use a stable, slim version of Python to avoid dependency issues.
FROM python:3.11-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Install system dependencies
# Includes dependencies needed by Chromium and Python packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    gnupg \
    unzip \
    # Basic build tools some python packages might need implicitly
    build-essential \
    # Needed for python packages like cryptography, Pillow
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg62-turbo-dev \
    # --- CHROMIUM v114 (Stable for Selenium 3.141) ---
    # Add Debian security repo for potentially older but compatible versions if needed
    # Note: Pinning versions like this makes the build less flexible but more stable for compatibility.
    # Check if chromium 114 is directly available first. Slim might be Bullseye or Bookworm.
    # If using Bookworm base:
    # RUN apt-get install -y chromium=114.* chromium-driver=114.* # Example, exact version might differ
    # If using Bullseye base (more likely for older compatibility):
    # May need to add bullseye-backports or specific older repo if 114 isn't in main bullseye
    # As a robust alternative, install latest available stable Chromium via apt, hoping it works better than google-chrome-stable
    chromium \
    chromium-driver \
    # --- Other dependencies ---
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
    xdg-utils && \
    # --- CLEANUP ---
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container.
WORKDIR /app

# Copy the requirements file first to leverage Docker's layer caching.
COPY requirements.txt .

# Install the Python dependencies.
# Ensure correct Selenium version is used (as per requirements.txt)
RUN pip install --no-cache-dir -r requirements.txt

# Add the /app directory to Python's import path
ENV PYTHONPATH=/app

# Copy the rest of your application code into the container.
COPY . .

# FIX: Move 'sources' inside 'lncrawl'
RUN mv /app/sources /app/lncrawl/sources && \
    find /app/lncrawl/sources -type d -exec touch {}/__init__.py \;

# Expose the port that the web service will listen on
EXPOSE 8080

# The command to run your web service (Gunicorn)
CMD gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 telegram_bot:server_app
