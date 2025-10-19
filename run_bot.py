import logging
import os

# Import the main initialization and polling functions
from telegram_bot import initialize_app, run_bot_polling

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s [%(threadName)s] - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        logger.info("Initializing Telegram bot application...")
        # Initialize creates and configures the app
        telegram_app = initialize_app()
        
        if telegram_app:
            logger.info("Starting Telegram bot polling...")
            # run_bot_polling starts the actual polling loop
            run_bot_polling(telegram_app)
        else:
            logger.fatal("Failed to initialize Telegram bot application. Exiting.")
            os._exit(1)
            
    except Exception as e:
        logger.fatal(f"A critical error occurred during bot startup: {e}", exc_info=True)
        os._exit(1)
