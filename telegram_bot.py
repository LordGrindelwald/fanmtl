import os
import logging
import time
import requests
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import shutil # Import shutil for directory removal

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
    global telegram_app # Need the global app instance
    if not telegram_app:
        logger.error("Telegram application not initialized.")
        return

    # --- FIX: Get the loop directly from the global app instance ---
    try:
        # The Application object should hold the loop it's running in
        loop = telegram_app.loop
        if not loop or not loop.is_running():
            # If run_polling hasn't fully started the loop yet, asyncio might provide it
            loop = asyncio.get_running_loop()
            if not loop or not loop.is_running():
                 raise RuntimeError("Event loop not available or not running.")
        logger.info(f"Using event loop: {loop} for crawl task.") # Add logging
    except Exception as e:
        logger.error(f"Could not retrieve a running event loop for crawl_and_send: {e}", exc_info=True)
        # Attempt to inform the user if possible
        if BOT_OWNER:
             # Running an async function from a sync function requires this:
             try:
                current_loop = asyncio.get_running_loop()
                if current_loop and current_loop.is_running():
                     asyncio.run_coroutine_threadsafe(context.bot.send_message(BOT_OWNER, f"Error starting crawl: Could not get event loop. {e}"), current_loop)
                else:
                     asyncio.run(context.bot.send_message(BOT_OWNER, f"Error starting crawl: Could not get event loop. {e}"))
             except RuntimeError: # No running loop in current thread
                 asyncio.run(context.bot.send_message(BOT_OWNER, f"Error starting crawl: Could not get event loop. {e}"))
             except Exception as report_e:
                 logger.error(f"Failed to report loop error to user: {report_e}")
        return
    # --- End Fix ---

    # Schedule the async function to run on the retrieved loop
    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)


async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data
    # Ensure status is updated *before* potential blocking operations
    bot_data['status'] = 'Initializing crawl...'
    bot_data['crawling'] = True
    logger.info("Starting a new crawl cycle...")

    try:
        # Instantiate the specific crawler directly
        site_crawler = FanMTLCrawler() # Corrected Case
        site_crawler.initialize()

        # Assuming the structure requires iterating through pages listed on the site.
        home_url = f"{WEBSITE}list/all/all-newstime-0.html" # Example start page
        bot_data['status'] = 'Fetching total page count...'
        soup = await asyncio.to_thread(site_crawler.get_soup, home_url) # Run sync I/O in thread

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
                bot_data['status'] = 'Crawl stopped by user.'
                break # Exit the page loop if crawling is stopped

            bot_data['status'] = f"Crawling page {page_num + 1} of {total_pages}..."
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = await asyncio.to_thread(site_crawler.get_soup, page_url) # Run sync I/O in thread

            # Find novel links on the current page (Selector might need adjustment)
            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            logger.info(f"Found {len(novel_links)} novels on page {page_num + 1}.")

            # Process novels on the current page (in reverse order found on page -> oldest first)
            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling'):
                    logger.info("Stopping novel processing on current page due to stop command.")
                    bot_data['status'] = 'Crawl stopped by user.'
                    break # Exit the novel loop if crawling is stopped
                novel_url = site_crawler.absolute_url(novel_link['href'])
                novel_title_element = novel_link.select_one('h4.novel-title')
                if novel_title_element:
                    novel_title = novel_title_element.text.strip()
                    logger.debug(f"Queueing processing for: {novel_title} ({novel_url})")
                    bot_data['status'] = f"Processing: {novel_title}"

                    # Process each novel asynchronously
                    await process_novel(novel_url, novel_title, context)
                    await asyncio.sleep(1) # Small delay between novels to avoid hammering
                else:
                    logger.warning(f"Could not find title for link: {novel_link.get('href')}")

            if not bot_data.get('crawling'):
                logger.info("Exiting page loop due to stop command.")
                break # Ensure exit after checking inner loop break condition


    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed with an error: {e}"
        if BOT_OWNER:
             await context.bot.send_message(BOT_OWNER, f"Crawl failed critically: {e}")
    finally:
        # Final status update only if not stopped by user
        if bot_data.get('crawling'): # Check if it finished naturally
            bot_data['status'] = 'Idle. Crawl finished.'
            logger.info("Crawl cycle finished naturally.")
            if BOT_OWNER:
                await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")
        else: # Status should already reflect stopping
             logger.info("Crawl cycle ended due to stop command.")
             if BOT_OWNER:
                 await context.bot.send_message(BOT_OWNER, "Crawl stopped as requested.")

        bot_data['crawling'] = False # Ensure crawling flag is reset


async def process_novel(novel_url, novel_title, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data
    try:
        processed_novel = novels_collection.find_one({"url": novel_url})

        # Instantiate the crawler for this specific novel
        novel_crawler = FanMTLCrawler() # Corrected Case
        novel_crawler.novel_url = novel_url # Set the URL
        # Initialization might involve network requests, run in thread
        await asyncio.to_thread(novel_crawler.initialize)

        # This method likely fetches novel metadata and chapter list, run in thread
        await asyncio.to_thread(novel_crawler.read_novel_info)
        latest_chapter_count = len(novel_crawler.chapters)
        logger.info(f"'{novel_title}' - Found {latest_chapter_count} chapters.")

        if processed_novel:
            # Check if there are new chapters
            if latest_chapter_count > processed_novel.get("chapter_count", 0):
                bot_data['status'] = f"Updating: {novel_title} ({latest_chapter_count} chapters)"
                logger.info(f"'{novel_title}' has an update. Downloading...")
                # Pass the instantiated novel_crawler to send_novel
                if await send_novel(novel_crawler, novel_url, context, caption="Updated"):
                    # Update chapter count only if sending was successful
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated", "last_checked": time.time()}}
                    )
                    logger.info(f"Successfully updated and sent '{novel_title}'.")
                else:
                    logger.warning(f"Failed to send update for '{novel_title}'. Database not updated.")
            else:
                 logger.info(f"'{novel_title}' is up to date ({latest_chapter_count} chapters).")
                 # Optionally update a 'last_checked' timestamp even if no update
                 novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"last_checked": time.time()}}
                    )

        else:
            # Novel is new
            bot_data['status'] = f"Downloading new: {novel_title} ({latest_chapter_count} chapters)"
            logger.info(f"Found new novel: '{novel_title}'. Downloading...")
            # Pass the instantiated novel_crawler to send_novel
            if await send_novel(novel_crawler, novel_url, context):
                # Insert record only if sending was successful
                novels_collection.insert_one({
                    "url": novel_url,
                    "title": novel_title,
                    "chapter_count": latest_chapter_count,
                    "status": "processed",
                    "first_added": time.time(),
                    "last_checked": time.time(),
                })
                logger.info(f"Successfully processed and sent new novel '{novel_title}'.")
            else:
                logger.warning(f"Failed to send new novel '{novel_title}'. Database not updated.")


    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        bot_data['status'] = f"Error processing: {novel_title}"
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
        # Ensure output path uses novel title for uniqueness
        app.output_path = os.path.join('/tmp', crawler.novel_title) # Use /tmp for Render compatibility
        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        logger.info(f"Packing '{crawler.novel_title}' to {app.output_path}...")
        # Run the potentially long-running synchronous packing task in a separate thread
        loop = asyncio.get_running_loop()
        # This assumes app.pack_epub() handles the downloading internally
        await loop.run_in_executor(None, app.pack_epub)
        logger.info(f"Finished packing '{crawler.novel_title}'.")


        # Get the output path *after* packing is done
        output_path = app.output_path # Use the path set on the app instance

        epub_found = False
        # Check if output_path exists before trying to list its contents
        if output_path and os.path.isdir(output_path):
            for filename in os.listdir(output_path):
                if filename.lower().endswith(".epub"): # Use lower() for case-insensitivity
                    file_path = os.path.join(output_path, filename)
                    logger.info(f"EPUB found: {file_path}. Attempting to send...")
                    context.application.bot_data['status'] = f"Sending: {filename}"
                    if BOT_OWNER:
                        try:
                            with open(file_path, "rb") as f:
                                await context.bot.send_document(
                                    chat_id=BOT_OWNER,
                                    document=f,
                                    filename=filename, # Explicitly set filename
                                    caption=f"{caption} {crawler.novel_title}".strip(), # Use novel title in caption
                                    read_timeout=300,  # Increased timeouts further
                                    write_timeout=300,
                                    connect_timeout=60
                                )
                            logger.info(f"Successfully sent {filename}")
                            epub_found = True
                        except Exception as send_err:
                             logger.error(f"Failed to send document {filename}: {send_err}", exc_info=True)
                             context.application.bot_data['status'] = f"Failed sending: {filename}"
                             if BOT_OWNER:
                                await context.bot.send_message(BOT_OWNER, f"Failed to send {filename}: {send_err}")
                             # Failure to send means overall failure for this novel attempt
                             epub_found = False # Explicitly set to false on error

                    else:
                        # If no BOT_OWNER, log that we would have sent it
                        logger.info(f"BOT_OWNER not set. Would have sent: {filename}")
                        epub_found = True # Consider it 'found' for logic purposes

                    # Clean up the generated epub file *after attempting* to send
                    # Moved to finally block to ensure cleanup even on send error

                    break # Send only the first epub found for this novel
            if not epub_found and os.path.isdir(output_path): # Check again if loop didn't find it
                 logger.warning(f"No EPUB file found in {output_path} for novel url {novel_url}")
                 context.application.bot_data['status'] = f"No EPUB for: {crawler.novel_title}"
        else:
             logger.error(f"Output path '{output_path}' not found or is not a directory for novel {novel_url}")
             context.application.bot_data['status'] = f"Output path error for: {crawler.novel_title}"


        return epub_found # Return True only if epub was found (and sent/logged successfully)

    except Exception as e:
        novel_name = crawler.novel_title if crawler and hasattr(crawler, 'novel_title') else novel_url
        logger.error(f"Failed during pack/send process for '{novel_name}': {e}", exc_info=True)
        context.application.bot_data['status'] = f"Error packing/sending: {novel_name}"
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error packing/sending '{novel_name}': {e}")
        return False
    finally:
        # Clean up the entire output directory for this novel
        if output_path and os.path.isdir(output_path):
            try:
                shutil.rmtree(output_path)
                logger.info(f"Cleaned up output directory: {output_path}")
            except Exception as clean_err:
                logger.warning(f"Could not clean up output directory {output_path}: {clean_err}")


# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Received /start command from user ID: {user_id}")
    if not user_id or user_id != BOT_OWNER:
        logger.warning(f"Unauthorized /start attempt by user ID: {user_id}")
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return

    if context.application.bot_data.get('crawling'):
        logger.info("Crawl already in progress. Replying to /start.")
        await update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        logger.info("Starting new crawl via /start command.")
        await update.message.reply_text("Crawl started. I will process all novels. Use /status to check my progress.")
        # Start the crawl process in a separate thread
        thread = Thread(target=crawl_and_send_sync, args=(context,), daemon=True)
        thread.start()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = update.effective_user.id if update.effective_user else None
     logger.debug(f"Received /status command from user ID: {user_id}") # Log status requests as debug
     if not user_id or user_id != BOT_OWNER:
        logger.warning(f"Unauthorized /status attempt by user ID: {user_id}")
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return
     current_status = context.application.bot_data.get('status', 'Idle.')
     await update.message.reply_text(f"**Status:** {current_status}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = update.effective_user.id if update.effective_user else None
     logger.info(f"Received /stop command from user ID: {user_id}")
     if not user_id or user_id != BOT_OWNER:
        logger.warning(f"Unauthorized /stop attempt by user ID: {user_id}")
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return

     if context.application.bot_data.get('crawling'):
        logger.info("Setting crawling flag to False due to /stop command.")
        context.application.bot_data['crawling'] = False
        context.application.bot_data['status'] = 'Stopping crawl...' # Update status immediately
        await update.message.reply_text("Stopping crawl... I will finish the current novel/page processing and then stop.")
     else:
        logger.info("Received /stop command but not currently crawling.")
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
    logger.info(f"Bot owner ID set to: {BOT_OWNER}")
except (ValueError, TypeError):
    logger.fatal("BOT_OWNER environment variable is not a valid integer.")
    raise RuntimeError("BOT_OWNER must be a valid integer.")


if not APP_URL:
    logger.warning("APP_URL environment variable is not set. Self-pinging will be disabled.")
else:
    logger.info(f"APP_URL set to: {APP_URL}")


# Create the Telegram Application
logger.info("Building Telegram Application...")
telegram_app = Application.builder().token(BOT_TOKEN).build()
logger.info("Telegram Application built.")


# Initialize bot state
telegram_app.bot_data['status'] = 'Idle. Ready to start.'
telegram_app.bot_data['crawling'] = False

# Register command handlers
logger.info("Registering command handlers...")
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("status", status_command))
telegram_app.add_handler(CommandHandler("stop", stop_command))
logger.info("Command handlers registered.")


# Start self-pinging in a background thread
if APP_URL:
    ping_thread = Thread(target=self_ping, daemon=True)
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started.")
else:
    logger.info("Self-pinging disabled as APP_URL is not set.")

# Start the bot polling in a separate thread
# Pass the created telegram_app instance to the thread
logger.info("Starting bot polling thread...")
bot_thread = Thread(target=run_bot_polling, args=(telegram_app,), name="TelegramPollingThread", daemon=True) # Give thread a name
bot_thread.start()

logger.info("Bot polling thread started. Gunicorn serving Flask app...")

# Gunicorn manages the main process and serves `server_app`.
# The bot polling and pinging run in background daemon threads.
