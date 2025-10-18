import os
import logging
import time
import requests
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Import necessary lncrawl components directly
from lncrawl.core.app import App # Still needed for packing
from lncrawl.sources.en.f.fanmtl import FanmtlCrawler # Import specific crawler

from pymongo import MongoClient, errors

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
BOT_OWNER = int(os.getenv("BOT_OWNER"))
WEBSITE = "https://fanmtl.com/"
APP_URL = os.getenv("APP_URL")

# --- Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# Flask app instance for Gunicorn
server_app = Flask(__name__)

# Global variable to hold the Telegram Application instance
telegram_app = None

# --- Database Connection ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_database("novel_bot")
    novels_collection = db.novels
    client.admin.command('ismaster')
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.fatal(f"Could not connect to MongoDB: {e}")
    # Raise error to stop Gunicorn from trying to run
    raise RuntimeError(f"Could not connect to MongoDB: {e}")

# --- Web Service Endpoint ---
@server_app.route('/')
def index():
    return "Bot is running."

@server_app.route('/health')
def health_check():
    return "OK", 200

# --- Keep-Alive Feature ---
def self_ping():
    while True:
        try:
            if APP_URL:
                requests.get(APP_URL + "/health", timeout=10)
                logger.info("Sent keep-alive ping to self.")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(14 * 60)

# --- Core Bot Logic (Runs in a separate thread) ---
def crawl_and_send_sync(context: ContextTypes.DEFAULT_TYPE):
    """Synchronous wrapper to run the async crawl process in a thread."""
    global telegram_app
    if not telegram_app:
        logger.error("Telegram application not initialized.")
        return
        
    loop = telegram_app.loop
    if not loop or not loop.is_running():
        # If loop is not running, it might be because it's managed by Application
        # Try getting it from the application again
        try:
            loop = context.application.loop
        except Exception:
            logger.error("Could not retrieve a running event loop.")
            return

    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)

async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        # Instantiate the specific crawler directly
        site_crawler = FanmtlCrawler() 
        site_crawler.initialize() 

        home_url = f"{WEBSITE}list/all/all-newstime-0.html"
        soup = site_crawler.get_soup(home_url) # Use the instantiated crawler
        last_page = 0
        page_links = soup.select('.pagination li a[href*="all-newstime-"]')
        if page_links:
            page_numbers = [int(link['href'].split('-')[-1].split('.')[0]) for link in page_links if link['href'].split('-')[-1].split('.')[0].isdigit()]
            if page_numbers:
                last_page = max(page_numbers)
        total_pages = last_page + 1
        logger.info(f"Determined there are {total_pages} pages to crawl.")

        for page_num in range(last_page, -1, -1):
            if not bot_data.get('crawling'):
                logger.info("Crawl was stopped by user command.")
                break

            bot_data['status'] = f"Crawling page {page_num + 1} of {total_pages}..."
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = site_crawler.get_soup(page_url) # Reuse the site crawler
            
            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            
            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling'):
                    break
                novel_url = site_crawler.absolute_url(novel_link['href'])
                novel_title = novel_link.select_one('h4.novel-title').text.strip()
                bot_data['status'] = f"Processing: {novel_title}"
                
                # Process each novel asynchronously
                await process_novel(novel_url, novel_title, context)
                await asyncio.sleep(1) # Use asyncio.sleep within async function
    
    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed with an error: {e}"
    finally:
        bot_data['status'] = 'Idle. Awaiting next command.'
        bot_data['crawling'] = False
        logger.info("Crawl cycle finished.")
        await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")

async def process_novel(novel_url, novel_title, context: ContextTypes.DEFAULT_TYPE):
    try:
        processed_novel = novels_collection.find_one({"url": novel_url})
        
        # Instantiate the crawler for this specific novel
        novel_crawler = FanmtlCrawler()
        novel_crawler.novel_url = novel_url # Set the URL
        novel_crawler.initialize() # Initialize it
        
        novel_crawler.read_novel_info() # Now read info
        latest_chapter_count = len(novel_crawler.chapters)

        if processed_novel:
            if latest_chapter_count > processed_novel.get("chapter_count", 0):
                context.application.bot_data['status'] = f"Updating: {novel_title}"
                logger.info(f"'{novel_title}' has an update. Downloading...")
                # Pass the instantiated novel_crawler to send_novel
                if await send_novel(novel_crawler, novel_url, context, caption="Updated"): 
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated"}}
                    )
        else:
            context.application.bot_data['status'] = f"Downloading new novel: {novel_title}"
            logger.info(f"Found new novel: '{novel_title}'. Downloading...")
            # Pass the instantiated novel_crawler to send_novel
            if await send_novel(novel_crawler, novel_url, context): 
                novels_collection.insert_one({
                    "url": novel_url,
                    "title": novel_title,
                    "chapter_count": latest_chapter_count,
                    "status": "processed"
                })

    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        await context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")

async def send_novel(crawler, novel_url, context: ContextTypes.DEFAULT_TYPE, caption=""):
    # Receives the specific crawler instance for this novel
    try:
        # Use a temporary App instance just for packing, passing the crawler
        app = App()
        app.crawler = crawler # Assign the already initialized crawler
        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        # Run the potentially long-running synchronous packing task in a separate thread
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, app.pack_epub) # Call pack_epub directly

        output_path = app.crawler.output_path
        
        for filename in os.listdir(output_path):
            if filename.endswith(".epub"):
                file_path = os.path.join(output_path, filename)
                logger.info(f"Sending file: {file_path}")
                with open(file_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=BOT_OWNER,
                        document=f,
                        caption=f"{caption} {filename}".strip(),
                        read_timeout=180, 
                        write_timeout=180, 
                        connect_timeout=60
                    )
                return True
        logger.warning(f"No EPUB file found in {output_path} for novel url {novel_url}")
        return False
    except Exception as e:
        novel_name = app.crawler.novel_title if app.crawler else novel_url
        logger.error(f"Failed to download or send '{novel_name}': {e}", exc_info=True)
        await context.bot.send_message(BOT_OWNER, f"Error sending '{novel_name}': {e}")
        return False

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get('crawling'):
        await update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        await update.message.reply_text("Crawl started. I will process all novels. Use /status to check my progress.")
        thread = Thread(target=crawl_and_send_sync, args=(context,), daemon=True)
        thread.start()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"**Status:** {context.application.bot_data.get('status', 'Idle.')}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get('crawling'):
        context.application.bot_data['crawling'] = False
        await update.message.reply_text("Stopping crawl... I will finish the current novel and then stop.")
    else:
        await update.message.reply_text("I am not currently crawling.")

# --- Initialization and Startup ---
def run_bot_polling(application: Application):
    """Runs the Telegram bot's polling loop."""
    logger.info("Starting Telegram bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

#
# --- STARTUP LOGIC (MOVED FROM main()) ---
# This code now runs when Gunicorn imports the file.
#

if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER]):
     logger.fatal("One or more environment variables are missing (BOT_TOKEN, MONGO_URI, BOT_OWNER). Bot cannot start.")
     raise RuntimeError("Missing essential environment variables.")

if not APP_URL:
    logger.warning("APP_URL environment variable is not set. Self-pinging will be disabled.")

# Create the Telegram Application
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Get the event loop *after* the Application is built
# We must create a new loop for the threads
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # If a loop is already running (e.g., in Gunicorn), create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

telegram_app.loop = loop

# Initialize bot state
telegram_app.bot_data['status'] = 'Idle. Ready to start.'
telegram_app.bot_data['crawling'] = False

# Register command handlers
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("status", status_command))
telegram_app.add_handler(CommandHandler("stop", stop_command))

# Start self-pinging in a background thread
if APP_URL:
    ping_thread = Thread(target=self_ping, daemon=True)
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started.")
else:
    logger.info("Self-pinging disabled as APP_URL is not set.")

# Start the bot polling in a separate thread
bot_thread = Thread(target=run_bot_polling, args=(telegram_app,), daemon=True)
bot_thread.start()

logger.info("Bot polling and web server are starting up...")

#
# DO NOT ADD `if __name__ == "__main__":` or `while True:`
# Gunicorn manages the process and serves `server_app`.
#
