# Use a stable, slim version of Python to avoid dependency issues.
FROM python:3.11-slim

# Set environment variables to prevent interactive prompts during package installations.
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies required by the crawler and Calibre.
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
    npm && \
    wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | \
sh /dev/stdin && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container.
WORKDIR /app

# Copy the requirements file first to leverage Docker's layer caching.
COPY requirements.txt .

# Install the Python dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# --- ADD THIS LINE TO FIX MODULE NOT FOUND ERROR ---
# Add the /app directory to Python's import path
ENV PYTHONPATH=/app

# Copy the rest of your application code into the container.
COPY . .
# Expose the port that the web service will listen on (Render provides this via $PORT).
EXPOSE 8080

# The command to run your web service (Gunicorn) which will also start the bot logic.
# Use the shell form of CMD to allow environment variable expansion for $PORT.
CMD gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 telegram_bot:server_app
