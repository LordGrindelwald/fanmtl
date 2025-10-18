import asyncio
import os
import threading
import logging
from flask import Flask

# THIS IS THE ONLY CHANGE NEEDED FOR YOUR ORIGINAL CODE TO WORK:
# By setting this environment variable BEFORE importing anything from lncrawl,
# we force the original, complex sources.py to load local files instead of
# trying to download them from the internet.
os.environ['LNCRAWL_MODE'] = 'dev'

# Now that the mode is set, we can safely import the necessary components.
from lncrawl.core.sources import load_sources
from lncrawl.bots.telegram import TelegramBot

# Configure basic logging so we can see what's happening
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def run_bot_in_background():
    """
    Initializes sources and runs the bot in a background thread.
    """
    logging.info("Bot thread started. Initializing sources in dev mode...")
    
    # This function will now find and load all the local crawler files
    # because of the environment variable set above.
    load_sources()
    logging.info("Sources initialized successfully.")

    logging.info("Setting up asyncio event loop for the new thread.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        logging.info("Initializing TelegramBot...")
        bot = TelegramBot()
        logging.info("TelegramBot initialized. Starting polling...")
        # The `start()` method in the original TelegramBot class is correct.
        bot.start()
    except Exception as e:
        logging.error(f"FATAL: An error occurred in the bot thread.", exc_info=True)

# Create the Flask web app for health checks
app = Flask(__name__)

@app.route('/')
def hello_world():
    """A simple route to keep the Render service alive."""
    return "Telegram bot is running in the background."

if __name__ == '__main__':
    logging.info("Starting web service...")
    
    # Start the bot in a background thread
    bot_thread = threading.Thread(target=run_bot_in_background)
    bot_thread.daemon = True
    bot_thread.start()
    logging.info("Bot thread has been dispatched.")

    # Start the web server in the main thread
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
