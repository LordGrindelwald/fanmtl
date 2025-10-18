import os
import logging
import time
from threading import Thread
from telegram import Bot
from telegram.ext import Updater, CommandHandler
from pymongo import MongoClient, errors
from lncrawl.core.app import App

# --- Configuration ---
# Load sensitive data from environment variables for security and deployment flexibility.
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
BOT_OWNER = int(os.getenv("BOT_OWNER"))
WEBSITE = "https://fanmtl.com/"

# --- Setup ---
# Configure logging to provide clear, timestamped output for debugging and monitoring.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Connection ---
# Establishes a robust connection to MongoDB, with error handling.
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_database("novel_bot")
    novels_collection = db.novels
    # The ismaster command is a lightweight way to verify the connection.
    client.admin.command('ismaster')
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.fatal(f"Could not connect to MongoDB: {e}")
    # The bot cannot run without a database, so we exit.
    exit(1)


# --- Core Bot Logic ---

def crawl_and_send(context):
    """
    Crawls the entire website from the last page to the first.
    This runs in a background thread to keep the Telegram bot responsive.
    """
    bot_data = context.bot_data
    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        app = App()
        app.initialize()

        # Dynamically determine the total number of pages at the start of each crawl.
        home_url = f"{WEBSITE}list/all/all-newstime-0.html"
        soup = app.crawler.get_soup(home_url)
        last_page = 0
        page_links = soup.select('.pagination li a[href*="all-newstime-"]')
        if page_links:
            page_numbers = [int(link['href'].split('-')[-1].split('.')[0]) for link in page_links if link['href'].split('-')[-1].split('.')[0].isdigit()]
            if page_numbers:
                last_page = max(page_numbers)
        total_pages = last_page + 1
        logger.info(f"Determined there are {total_pages} pages to crawl.")

        # Crawl backwards to process oldest novels first and handle new additions gracefully.
        for page_num in range(last_page, -1, -1):
            if not bot_data.get('crawling'):
                logger.info("Crawl was stopped by user command.")
                break

            bot_data['status'] = f"Crawling page {page_num + 1} of {total_pages}..."
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = app.crawler.get_soup(page_url)
            
            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            
            # Process novels on the current page from oldest to newest.
            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling'):
                    break
                novel_url = app.crawler.absolute_url(novel_link['href'])
                novel_title = novel_link.select_one('h4.novel-title').text.strip()
                bot_data['status'] = f"Processing: {novel_title}"

                process_novel(novel_url, novel_title, app, context)
                time.sleep(1) # A polite delay to avoid overwhelming the website's server.
    
    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed with an error: {e}"
    finally:
        bot_data['status'] = 'Idle. Awaiting next command.'
        bot_data['crawling'] = False
        logger.info("Crawl cycle finished.")
        context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")


def process_novel(novel_url, novel_title, app, context):
    """Checks a novel against the database and triggers a download or update if necessary."""
    try:
        # Check if the novel has been processed before.
        processed_novel = novels_collection.find_one({"url": novel_url})
        
        # Get the latest chapter count from the website.
        app.crawler.novel_url = novel_url
        app.crawler.read_novel_info()
        latest_chapter_count = len(app.crawler.chapters)

        if processed_novel:
            # If the novel exists, check if there are new chapters.
            if latest_chapter_count > processed_novel.get("chapter_count", 0):
                context.bot_data['status'] = f"Updating: {novel_title}"
                logger.info(f"'{novel_title}' has an update. Downloading...")
                if send_novel(app, novel_url, context, caption="Updated"):
                    # Only update the database if the file was sent successfully.
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated"}}
                    )
        else:
            # If the novel is new, download it and record it in the database.
            context.bot_data['status'] = f"Downloading new novel: {novel_title}"
            logger.info(f"Found new novel: '{novel_title}'. Downloading...")
            if send_novel(app, novel_url, context):
                novels_collection.insert_one({
                    "url": novel_url,
                    "title": novel_title,
                    "chapter_count": latest_chapter_count,
                    "status": "processed"
                })

    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")


def send_novel(app, novel_url, context, caption=""):
    """Downloads a novel as an EPUB and sends it to the bot owner via Telegram."""
    try:
        # Force the app to treat the novel as a single volume for filename purposes.
        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        # Use the app's built-in packing method to create the EPUB.
        app.pack_by_url(novel_url, {"epub"})
        output_path = app.crawler.output_path
        
        for filename in os.listdir(output_path):
            if filename.endswith(".epub"):
                file_path = os.path.join(output_path, filename)
                logger.info(f"Sending file: {file_path}")
                with open(file_path, "rb") as f:
                    context.bot.send_document(
                        chat_id=BOT_OWNER,
                        document=f,
                        caption=f"{caption} {filename}".strip(),
                        timeout=180  # Generous timeout for large files.
                    )
                return True # Indicate success.
        return False # Indicate failure if no EPUB was found.
    except Exception as e:
        logger.error(f"Failed to download or send '{app.crawler.novel_title}': {e}", exc_info=True)
        context.bot.send_message(BOT_OWNER, f"Error sending '{app.crawler.novel_title}': {e}")
        return False # Indicate failure.


# --- Telegram Command Handlers ---

def start(update, context):
    """Handler for the /start command. Initiates the crawl in a new thread."""
    if context.bot_data.get('crawling'):
        update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        update.message.reply_text("Crawl started. I will process all novels from the site. Use /status to check my progress.")
        thread = Thread(target=crawl_and_send, args=(context,))
        thread.daemon = True
        thread.start()

def status(update, context):
    """Handler for the /status command. Reports the bot's current activity."""
    update.message.reply_text(f"**Status:** {context.bot_data.get('status', 'Idle.')}")

def stop(update, context):
    """Handler for the /stop command. Gracefully stops the current crawl."""
    if context.bot_data.get('crawling'):
        context.bot_data['crawling'] = False
        update.message.reply_text("Stopping crawl... I will finish the current novel and then stop.")
    else:
        update.message.reply_text("I am not currently crawling.")

def main():
    """Starts the Telegram bot, sets up handlers, and begins polling."""
    if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER]):
        logger.fatal("One or more environment variables (BOT_TOKEN, MONGO_URI, BOT_OWNER) are missing. Bot cannot start.")
        return

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Initialize bot state.
    dp.bot_data['status'] = 'Idle. Ready to start.'
    dp.bot_data['crawling'] = False

    # Register all command handlers.
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("stop", stop))

    # Start the bot.
    updater.start_polling()
    logger.info("Telegram bot is now running and listening for commands.")
    updater.idle()


if __name__ == "__main__":
    main()
