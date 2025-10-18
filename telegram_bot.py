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
from lncrawl.core.app import App
from lncrawl.core.sources import SITES # Import SITES to find the correct crawler class

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
# Rename Flask app to avoid conflict if needed, though 'app' is standard for gunicorn
server_app = Flask(__name__) # Use server_app for Flask

# --- Database Connection ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_database("novel_bot")
    novels_collection = db.novels
    client.admin.command('ismaster')
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.fatal(f"Could not connect to MongoDB: {e}")
    exit(1)

# --- Web Service Endpoint ---
@server_app.route('/') # Use server_app
def index():
    return "Bot is running."

@server_app.route('/health') # Use server_app
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
    loop = context.application.loop
    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)

async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        # Correctly get the crawler instance
        app = App() # Main App instance for session/helpers
        crawler_cls = SITES.get_crawler(WEBSITE)
        if not crawler_cls:
            logger.error("Could not find a crawler class for the website.")
            bot_data['status'] = 'Error: Crawler class not found.'
            await context.bot.send_message(BOT_OWNER, "Error: Crawler class not found.")
            return
        
        # Instantiate crawler for initial page check
        site_crawler = crawler_cls() 
        site_crawler.initialize() # Initialize with session etc.

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
                await asyncio.sleep(1)
    
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
        
        # Use a new App instance for each novel to encapsulate state
        app = App()
        # Explicitly set the novel URL *before* other operations
        app.novel_url = novel_url
        
        # Get the crawler specific to this novel URL
        app.crawler = app.get_crawler_instance()
        if not app.crawler:
             logger.error(f"Could not get crawler for novel: {novel_url}")
             await context.bot.send_message(BOT_OWNER, f"Error: Could not get crawler for {novel_title}")
             return

        app.crawler.read_novel_info()
        latest_chapter_count = len(app.crawler.chapters)

        if processed_novel:
            if latest_chapter_count > processed_novel.get("chapter_count", 0):
                context.application.bot_data['status'] = f"Updating: {novel_title}"
                logger.info(f"'{novel_title}' has an update. Downloading...")
                if await send_novel(app, novel_url, context, caption="Updated"):
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated"}}
                    )
        else:
            context.application.bot_data['status'] = f"Downloading new novel: {novel_title}"
            logger.info(f"Found new novel: '{novel_title}'. Downloading...")
            if await send_novel(app, novel_url, context):
                novels_collection.insert_one({
                    "url": novel_url,
                    "title": novel_title,
                    "chapter_count": latest_chapter_count,
                    "status": "processed"
                })

    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        await context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")

async def send_novel(app, novel_url, context: ContextTypes.DEFAULT_TYPE, caption=""):
    try:
        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        # Run the potentially long-running synchronous packing task in a separate thread
        # managed by asyncio to avoid blocking the event loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, app.pack_by_url, novel_url, {"epub"})
        
        # Ensure crawler object exists before accessing output_path
        if not app.crawler:
            logger.error(f"Crawler object is None during send_novel for {novel_url}")
            return False

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
        logger.warning(f"No EPUB file found in {output_path} for {novel_title}")
        return False
    except Exception as e:
        logger.error(f"Failed to download or send '{app.crawler.novel_title if app.crawler else novel_url}': {e}", exc_info=True)
        await context.bot.send_message(BOT_OWNER, f"Error sending '{app.crawler.novel_title if app.crawler else novel_url}': {e}")
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

# --- Main Setup and Run ---
def run_flask():
    """Runs the Flask app using Gunicorn."""
    # Gunicorn expects the app callable as module:variable
    # We don't run it directly here, but configure it for the Procfile/start command
    pass # Gunicorn will be started by the Procfile or start command

def main():
    if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER, APP_URL]):
        logger.fatal("One or more environment variables are missing (BOT_TOKEN, MONGO_URI, BOT_OWNER, APP_URL).")
        return

    # Create the Telegram Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Store the running event loop in the application context
    application.loop = asyncio.get_event_loop()

    # Initialize bot state
    application.bot_data['status'] = 'Idle. Ready to start.'
    application.bot_data['crawling'] = False

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # Start self-pinging in a background thread
    ping_thread = Thread(target=self_ping, daemon=True)
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started.")

    # Run the bot's main loop (this blocks the main thread)
    logger.info("Telegram bot is now running...")
    application.run_polling()

if __name__ == "__main__":
    # Start Flask app using gunicorn via Render's start command or Procfile
    # The main() function now only starts the bot and ping thread.
    main()
