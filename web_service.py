import os
import logging
from threading import Thread

# Import the Flask app and the ping function from the main bot file
from telegram_bot import server_app, self_ping

logger = logging.getLogger(__name__)

# --- Start Keep-Alive Thread ---
# This is done here so that it only runs in the web service process
APP_URL = os.getenv("APP_URL")
if APP_URL:
    ping_thread = Thread(target=self_ping, name="SelfPingThread", daemon=True)
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started for web process.")
else:
    logger.warning("APP_URL not set, self-pinging disabled for web process.")

# Gunicorn will automatically find and run 'server_app'
