#!/bin/bash

# Start the Gunicorn web server in the background
echo "Starting Gunicorn web server..."
gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 8 --timeout 0 web_service:server_app &

# Wait for a few seconds to let Gunicorn start
sleep 5

# Start the Telegram bot runner in the foreground
# This will keep the container running
echo "Starting Telegram bot polling..."
python3 /app/run_bot.py
