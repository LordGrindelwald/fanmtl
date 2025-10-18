import os
import logging
import time
import requests
import asyncio
import uvicorn
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from lncrawl.core.app import App
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
flask_app = Flask(__name__)

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
@flask_app.route('/')
def index():
    return "Bot is running."

@flask_app.route('/health')
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

# --- Core Bot Logic ---
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
        app = App()
        crawler = app.get_crawler(WEBSITE)
        if not crawler:
            logger.error("Could not find a crawler for the website.")
            bot_data['status'] = 'Error: Crawler not found.'
            return

        home_url = f"{WEBSITE}list/all/all-newstime-0.html"
        soup = crawler.get_soup(home_url)
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
            page_soup = crawler.get_soup(page_url)
            
            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            
            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling'):
                    break
                novel_url = crawler.absolute_url(novel_link['href'])
                novel_title = novel_link.select_one('h4.novel-title').text.strip()
                bot_data['status'] = f"Processing: {novel_title}"
                
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
        
        app = App()
        app.crawler = app.get_crawler(novel_url)
        if not app.crawler:
             logger.error(f"Could not get crawler for novel: {novel_url}")
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

        app.pack_by_url(novel_url, {"epub"})
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
                        timeout=180
                    )
                return True
        return False
    except Exception as e:
        logger.error(f"Failed to download or send '{app.crawler.novel_title}': {e}", exc_info=True)
        await context.bot.send_message(BOT_OWNER, f"Error sending '{app.crawler.novel_title}': {e}")
        return False

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get('crawling'):
        await update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        await update.message.reply_text("Crawl started. I will process all novels. Use /status to check my progress.")
        thread = Thread(target=crawl_and_send_sync, args=(context,))
        thread.daemon = True
        thread.start()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"**Status:** {context.application.bot_data.get('status', 'Idle.')}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get('crawling'):
        context.application.bot_data['crawling'] = False
        await update.message.reply_text("Stopping crawl... I will finish the current novel and then stop.")
    else:
        await update.message.reply_text("I am not currently crawling.")

async def main():
    if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER, APP_URL]):
        logger.fatal("One or more environment variables are missing (BOT_TOKEN, MONGO_URI, BOT_OWNER, APP_URL).")
        return

    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Initialize bot state
    application.bot_data['status'] = 'Idle. Ready to start.'
    application.bot_data['crawling'] = False

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # Start self-pinging in a background thread
    ping_thread = Thread(target=self_ping)
    ping_thread.daemon = True
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started.")

    # Start Flask server in a background thread using uvicorn
    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame) -> None:
            return super().handle_exit(sig, frame)

    config = uvicorn.Config(app=flask_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), log_level="info")
    server = Server(config)
    
    server_thread = Thread(target=server.run)
    server_thread.daemon = True
    server_thread.start()
    logger.info("Flask web service started.")

    # Run the bot
    logger.info("Telegram bot is now running...")
    # This will run forever until the process is stopped.
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
