import logging
import os
from telegram import Update

# Import the main initialization function
from telegram_bot import initialize_app

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
            # Application.run_polling() is a blocking call.
            # It will create and manage its own event loop.
            # This loop will be accessible via telegram_app.loop for our threads.
            telegram_app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=None # Handle stops gracefully in Docker
            )
            logger.warning("Telegram bot polling has stopped.")
        else:
            logger.fatal("Failed to initialize Telegram bot application. Exiting.")
            os._exit(1)
            
    except Exception as e:
        logger.fatal(f"A critical error occurred during bot startup: {e}", exc_info=True)
        os._exit(1)
