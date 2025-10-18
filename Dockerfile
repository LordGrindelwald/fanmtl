# Use a stable, slim version of Python to avoid dependency issues.
FROM python:3.11-slim

# Set environment variables to prevent interactive prompts during package installations.
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    tar \
    xz-utils \
    libxext6 \
    libxrender1 \
    libxtst6 \
    libfreetype6 \
    libfontconfig1 \
    libegl1 \
    libopengl0 \
    libxcb-cursor0 \
    nodejs \
    npm \
    # --- ADD CHROME DEPENDENCIES ---
    gnupg \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgcc1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    wget \
    xdg-utils && \
    # --- INSTALL GOOGLE CHROME ---
    wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && \
    apt-get install -y ./google-chrome-stable_current_amd64.deb && \
    rm google-chrome-stable_current_amd64.deb && \
    # --- INSTALL CALIBRE ---
    wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sh /dev/stdin && \
    # --- CLEANUP ---
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container.
WORKDIR /app

# Copy the requirements file first to leverage Docker's layer caching.
COPY requirements.txt .

# Install the Python dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Add the /app directory to Python's import path so modules like lncrawl can be found
ENV PYTHONPATH=/app

# Copy the rest of your application code into the container.
COPY . .

# FIX: Move 'sources' inside 'lncrawl' to match the import paths
# and create __init__.py files to make them all importable packages.
RUN mv /app/sources /app/lncrawl/sources && \
    find /app/lncrawl/sources -type d -exec touch {}/__init__.py \;

# Expose the port that the web service will listen on (Render provides this via $PORT).
# Note: Render ignores this EXPOSE line for web services, but it's good practice.
EXPOSE 8080

# The command to run your web service (Gunicorn) which will also start the bot logic.
# Render uses the Procfile for web services, but this CMD is a fallback/local standard.
CMD gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 telegram_bot:server_app
