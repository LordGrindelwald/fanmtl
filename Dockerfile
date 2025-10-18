# Use a stable, slim version of Python to avoid dependency issues.
FROM python:3.11-slim

# Set environment variables to prevent interactive prompts during package installations.
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies required by the crawler and Calibre.
# Added libegl1 and libopengl0 to satisfy Calibre's installation requirements.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libxext6 \
    libxrender1 \
    libxtst6 \
    libfreetype6 \
    libfontconfig1 \
    libegl1 \
    libopengl0 \
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

# Install the Python dependencies specified in your requirements file.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container.
COPY . .

# Expose the port that the web service will listen on. Render provides this as a PORT env var.
EXPOSE 8080

# The command to run your web service and bot when the container starts.
CMD ["python", "telegram_bot.py"]
