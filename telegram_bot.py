import os
import logging
import time
import requests
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- DIRECTLY MODIFY PYTHON PATH ---
import sys
import os
# Add the '/app' directory (where lncrawl lives) to Python's search path
app_path = '/app'
if app_path not in sys.path:
    sys.path.insert(0, app_path)
# --- END PATH MODIFICATION ---


# Import necessary lncrawl components directly
# This import should now work if '/app' is in sys.path
from lncrawl.core.app import App # Still needed for packing
# FIX: Corrected Case Sensitivity
from lncrawl.sources.en.f.fanmtl import FanMTLCrawler # Import specific crawler

from pymongo import MongoClient, errors

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
# Get BOT_OWNER as string first for validation
BOT_OWNER_STR = os.getenv("BOT_OWNER")
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
BOT_OWNER = None # Initialize BOT_OWNER

# --- Database Connection ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_database("novel_bot")
    novels_collection = db.novels
    client.admin.command('ismaster') # The ismaster command is cheap and does not require auth.
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
        time.sleep(14 * 60) # Sleep for 14 minutes (just under Render's 15 min timeout)

# --- Core Bot Logic (Runs in a separate thread) ---
def crawl_and_send_sync(context: ContextTypes.DEFAULT_TYPE):
    """Synchronous wrapper to run the async crawl process in a thread."""
    global telegram_app
    if not telegram_app:
        logger.error("Telegram application not initialized.")
        return

    # FIX: Get the loop from the application, as it's managed by Application now.
    try:
        loop = context.application.loop
        if not loop or not loop.is_running():
            raise RuntimeError("Event loop not available or not running.")
    except Exception as e:
        logger.error(f"Could not retrieve a running event loop for crawl_and_send: {e}")
        return

    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)


async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data
    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        # Instantiate the specific crawler directly
        site_crawler = FanMTLCrawler() # Corrected Case
        site_crawler.initialize()

        # Assuming the structure requires iterating through pages listed on the site.
        # This part might need adjustment based on FanMTLCrawler's actual capabilities
        # if it has a more direct way to get all novels.
        home_url = f"{WEBSITE}list/all/all-newstime-0.html" # Example start page
        soup = site_crawler.get_soup(home_url) # Use the instantiated crawler's method

        # Determine the total number of pages (Example logic, adapt as needed)
        last_page = 0
        page_links = soup.select('.pagination li a[href*="all-newstime-"]')
        if page_links:
            page_numbers = [int(link['href'].split('-')[-1].split('.')[0]) for link in page_links if link['href'].split('-')[-1].split('.')[0].isdigit()]
            if page_numbers:
                last_page = max(page_numbers)
        total_pages = last_page + 1
        logger.info(f"Determined there are {total_pages} pages to crawl.")

        # Crawl pages in reverse order (newest first based on URL structure)
        for page_num in range(last_page, -1, -1):
            if not bot_data.get('crawling'):
                logger.info("Crawl was stopped by user command.")
                break # Exit the page loop if crawling is stopped

            bot_data['status'] = f"Crawling page {page_num + 1} of {total_pages}..."
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = site_crawler.get_soup(page_url) # Reuse the site crawler instance

            # Find novel links on the current page (Selector might need adjustment)
            novel_links = page_soup.select('ul.novel-list li.novel-item a')

            # Process novels on the current page (in reverse order found on page -> oldest first)
            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling'):
                    break # Exit the novel loop if crawling is stopped
                novel_url = site_crawler.absolute_url(novel_link['href'])
                novel_title_element = novel_link.select_one('h4.novel-title')
                if novel_title_element:
                    novel_title = novel_title_element.text.strip()
                    bot_data['status'] = f"Processing: {novel_title}"

                    # Process each novel asynchronously
                    await process_novel(novel_url, novel_title, context)
                    await asyncio.sleep(1) # Small delay between novels
                else:
                    logger.warning(f"Could not find title for link: {novel_link.get('href')}")

            if not bot_data.get('crawling'):
                break # Ensure exit after checking inner loop break condition


    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed with an error: {e}"
        if BOT_OWNER:
             # Use await inside async function
             await context.bot.send_message(BOT_OWNER, f"Crawl failed critically: {e}")
    finally:
        bot_data['status'] = 'Idle. Awaiting next command.'
        bot_data['crawling'] = False
        logger.info("Crawl cycle finished.")
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")

async def process_novel(novel_url, novel_title, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    try:
        processed_novel = novels_collection.find_one({"url": novel_url})

        # Instantiate the crawler for this specific novel
        novel_crawler = FanMTLCrawler() # Corrected Case
        novel_crawler.novel_url = novel_url # Set the URL
        novel_crawler.initialize() # Initialize it for this specific novel

        # This method likely fetches novel metadata and chapter list
        novel_crawler.read_novel_info()
        latest_chapter_count = len(novel_crawler.chapters)

        if processed_novel:
            # Check if there are new chapters
            if latest_chapter_count > processed_novel.get("chapter_count", 0):
                context.application.bot_data['status'] = f"Updating: {novel_title}"
                logger.info(f"'{novel_title}' has an update ({latest_chapter_count} chapters). Downloading...")
                # Pass the instantiated novel_crawler to send_novel
                if await send_novel(novel_crawler, novel_url, context, caption="Updated"):
                    # Update chapter count only if sending was successful
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated"}}
                    )
            # Optional: Log if novel is up-to-date
            # else:
            #     logger.info(f"'{novel_title}' is up to date.")
        else:
            # Novel is new
            context.application.bot_data['status'] = f"Downloading new novel: {novel_title}"
            logger.info(f"Found new novel: '{novel_title}' ({latest_chapter_count} chapters). Downloading...")
            # Pass the instantiated novel_crawler to send_novel
            if await send_novel(novel_crawler, novel_url, context):
                # Insert record only if sending was successful
                novels_collection.insert_one({
                    "url": novel_url,
                    "title": novel_title,
                    "chapter_count": latest_chapter_count,
                    "status": "processed"
                })

    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")

async def send_novel(crawler, novel_url, context: ContextTypes.DEFAULT_TYPE, caption=""):
    # Receives the specific crawler instance for this novel
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    app = None # Initialize app to None
    output_path = None # Initialize output_path
    try:
        # Use a temporary App instance just for packing, passing the crawler
        app = App()
        app.crawler = crawler # Assign the already initialized and info-loaded crawler
        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        # Run the potentially long-running synchronous packing task in a separate thread
        loop = asyncio.get_running_loop()
        # This assumes app.pack_epub() handles the downloading internally
        await loop.run_in_executor(None, app.pack_epub)

        # Get the output path *after* packing is done
        output_path = app.crawler.output_path

        epub_found = False
        # Check if output_path exists before trying to list its contents
        if output_path and os.path.isdir(output_path):
            for filename in os.listdir(output_path):
                if filename.lower().endswith(".epub"): # Use lower() for case-insensitivity
                    file_path = os.path.join(output_path, filename)
                    logger.info(f"Sending file: {file_path}")
                    if BOT_OWNER:
                        try:
                            with open(file_path, "rb") as f:
                                await context.bot.send_document(
                                    chat_id=BOT_OWNER,
                                    document=f,
                                    caption=f"{caption} {filename}".strip(),
                                    read_timeout=180,  # Increased timeouts for potentially large files
                                    write_timeout=180,
                                    connect_timeout=60
                                )
                            logger.info(f"Successfully sent {filename}")
                            epub_found = True
                        except Exception as send_err:
                             logger.error(f"Failed to send document {filename}: {send_err}", exc_info=True)
                             if BOT_OWNER:
                                await context.bot.send_message(BOT_OWNER, f"Failed to send {filename}: {send_err}")
                             # Decide if you want to return False here or try cleaning up anyway
                             # return False # Uncomment if failure to send means overall failure

                    else:
                        # If no BOT_OWNER, log that we would have sent it
                        logger.info(f"BOT_OWNER not set. Would have sent: {filename}")
                        epub_found = True # Consider it 'found' for logic purposes

                    # Clean up the generated epub *after attempting* to send
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logger.info(f"Removed temporary file: {file_path}")
                        except OSError as rm_err:
                            logger.warning(f"Could not remove temporary file {file_path}: {rm_err}")

                    break # Send only the first epub found for this novel
            if not epub_found:
                 logger.warning(f"No EPUB file found in {output_path} for novel url {novel_url}")
        else:
             logger.error(f"Output path '{output_path}' not found or is not a directory for novel {novel_url}")


        return epub_found # Return True only if epub was found (and sent/logged)

    except Exception as e:
        novel_name = crawler.novel_title if crawler and hasattr(crawler, 'novel_title') else novel_url
        logger.error(f"Failed to download or send '{novel_name}': {e}", exc_info=True)
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error sending '{novel_name}': {e}")
        return False
    finally:
        # Optional: Clean up the entire output directory if it exists,
        # but be careful if multiple downloads could happen concurrently to the same base path.
        # This cleanup might be better handled after the entire crawl cycle.
        # if output_path and os.path.isdir(output_path):
        #     try:
        #         shutil.rmtree(output_path)
        #         logger.info(f"Cleaned up output directory: {output_path}")
        #     except Exception as clean_err:
        #         logger.warning(f"Could not clean up output directory {output_path}: {clean_err}")
        pass


# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != BOT_OWNER:
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return

    if context.application.bot_data.get('crawling'):
        await update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        await update.message.reply_text("Crawl started. I will process all novels. Use /status to check my progress.")
        # Ensure the thread starts the synchronous wrapper
        thread = Thread(target=crawl_and_send_sync, args=(context,), daemon=True)
        thread.start()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     if not update.effective_user or update.effective_user.id != BOT_OWNER:
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return
     await update.message.reply_text(f"**Status:** {context.application.bot_data.get('status', 'Idle.')}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     if not update.effective_user or update.effective_user.id != BOT_OWNER:
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return

     if context.application.bot_data.get('crawling'):
        context.application.bot_data['crawling'] = False
        context.application.bot_data['status'] = 'Stopping crawl...' # Update status immediately
        await update.message.reply_text("Stopping crawl... I will finish the current novel and then stop.")
     else:
        await update.message.reply_text("I am not currently crawling.")

# --- Initialization and Startup ---
def run_bot_polling(application: Application):
    """Runs the Telegram bot's polling loop."""
    logger.info("Starting Telegram bot polling...")
    try:
        # --- FIX: Set event loop for *this* thread ---
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("Created and set new event loop for bot polling thread.")
        # --- End Fix ---

        # --- FIX: Disable PTB's signal handlers in the background thread ---
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            stop_signals=None # <<< ADDED THIS LINE
        )
        # --- End Fix ---

    except Exception as e:
        logger.critical(f"Bot polling loop failed critically: {e}", exc_info=True)
        # Force exit the entire process if polling fails, Gunicorn should restart it.
        os._exit(1)

#
# --- STARTUP LOGIC ---
# This code runs when Gunicorn imports the file.
#

if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER_STR]):
     logger.fatal("One or more environment variables are missing (BOT_TOKEN, MONGO_URI, BOT_OWNER). Bot cannot start.")
     raise RuntimeError("Missing essential environment variables.")

# Convert BOT_OWNER to int *after* checking it exists
try:
    BOT_OWNER = int(BOT_OWNER_STR)
except (ValueError, TypeError):
    logger.fatal("BOT_OWNER environment variable is not a valid integer.")
    raise RuntimeError("BOT_OWNER must be a valid integer.")


if not APP_URL:
    logger.warning("APP_URL environment variable is not set. Self-pinging will be disabled.")

# Create the Telegram Application
telegram_app = Application.builder().token(BOT_TOKEN).build()

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
# Pass the created telegram_app instance to the thread
bot_thread = Thread(target=run_bot_polling, args=(telegram_app,), daemon=True)
bot_thread.start()

logger.info("Bot polling thread started. Gunicorn serving Flask app...")

# Gunicorn manages the main process and serves `server_app`.
# The bot polling and pinging run in background daemon threads.
