# Use a stable, slim version of Python to avoid dependency issues.
FROM python:3.11-slim

# Set environment variables to prevent interactive prompts during package installations.
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies required by the crawler and Calibre.
# This comprehensive list includes all libraries required by Calibre's installer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Essential tools
    wget \
    ca-certificates \
    tar \
    xz-utils \
    # Calibre's graphical dependencies (needed even for CLI)
    libxext6 \
    libxrender1 \
    libxtst6 \
    libfreetype6 \
    libfontconfig1 \
    libegl1 \
    libopengl0 \
    libxcb-cursor0 \
    # Node.js for potential Cloudflare challenges
    nodejs \
    npm && \
    # Download and run the official Calibre installer for Linux.
    wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sh /dev/stdin && \
    # Clean up apt caches to keep the final image size smaller.
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container.
WORKDIR /app

# Copy the requirements file first to leverage Docker's layer caching.
COPY requirements.txt .

# Install the Python dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container.
COPY . .

# Expose the port that the web service will listen on.
EXPOSE 8080

# The command to run your web service and bot when the container starts.
CMD ["python", "telegram_bot.py"]
