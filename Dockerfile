# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /home/appuser/app

# Install dependencies, including Calibre
RUN apt-get update && apt-get install -y \
    build-essential \
    libpoppler-cpp-dev \
    pkg-config \
    python3-dev \
    libjpeg-dev \
    libffi-dev \
    zlib1g-dev \
    --no-install-recommends && \
    wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sh /dev/stdin

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# Run the web service, which will also start the bot
CMD ["python3", "web_service.py"]
