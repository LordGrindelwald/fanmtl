import os
import logging
import time
import requests
import asyncio
from threading import Thread, current_thread # Import current_thread
from flask import Flask
from telegram import Update
# <<< MODIFIED IMPORTS >>>
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
# <<< END MODIFIED IMPORTS >>>
import shutil # Import shutil for directory removal
import re # Import re
from datetime import datetime # Import datetime

# --- DIRECTLY MODIFY PYTHON PATH ---
import sys
import os
# Add the '/app' directory (where lncrawl lives) to Python's search path
app_path = '/app'
if app_path not in sys.path:
    sys.path.insert(0, app_path)
# --- END PATH MODIFICATION ---


# Import necessary lncrawl components directly
from lncrawl.core.app import App # Still needed for packing
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
    format='%(asctime)s - %(name)s - %(levelname)s [%(threadName)s] - %(message)s', # Added threadName
    level=logging.INFO # Keep level at INFO for now
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
                # Use the health check endpoint for pinging
                ping_url = f"{APP_URL.rstrip('/')}/health"
                requests.get(ping_url, timeout=10)
                logger.info(f"Sent keep-alive ping to self: {ping_url}")
            else:
                # Log only once if APP_URL is not set, then sleep
                logger.warning("APP_URL not set, self-pinging disabled.")
                time.sleep(14 * 60) # Still sleep to avoid busy-looping if APP_URL is missing
                continue # Skip the rest of the loop
        except requests.exceptions.RequestException as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in self_ping: {e}", exc_info=True)

        # Sleep regardless of success or failure (unless APP_URL was missing)
        time.sleep(14 * 60) # Sleep for 14 minutes


# --- Core Bot Logic (Runs in a separate thread) ---
def crawl_and_send_sync(context: ContextTypes.DEFAULT_TYPE):
    """Synchronous wrapper to run the async crawl process in a thread."""
    global telegram_app # Need the global app instance
    if not telegram_app:
        logger.error("Telegram application not initialized.")
        return

    # --- FIX: Try accessing the loop explicitly set on the app instance ---
    try:
        # Check if the loop attribute was set by run_bot_polling
        if hasattr(telegram_app, 'loop') and telegram_app.loop:
            loop = telegram_app.loop
            if not loop.is_running():
                 raise RuntimeError("Event loop found but is not running.")
            logger.info(f"Using event loop from telegram_app.loop: {loop} for crawl task.")
        else:
             # Fallback if the attribute wasn't set (shouldn't happen with the fix)
             raise RuntimeError("telegram_app.loop attribute not found or is None.")

    except Exception as e:
        logger.error(f"Could not retrieve a running event loop for crawl_and_send: {e}", exc_info=True)
        # Attempt to inform the user if possible (using asyncio.run as this is a sync func)
        if BOT_OWNER:
             try:
                 asyncio.run(context.bot.send_message(BOT_OWNER, f"Error starting crawl: Could not get event loop. {e}"))
             except Exception as report_e:
                 logger.error(f"Failed to report loop error to user: {report_e}")
        return
    # --- End Fix ---

    # Schedule the async function to run on the retrieved loop
    logger.info(f"Scheduling crawl_and_send coroutine on loop {loop}")
    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)


async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data
    # Ensure status is updated *before* potential blocking operations
    bot_data['status'] = 'Initializing crawl...'
    bot_data['crawling'] = True # Set crawling flag immediately
    logger.info("Starting a new crawl cycle...")

    try:
        # Instantiate the specific crawler directly
        site_crawler = FanMTLCrawler()
        # Initialization might involve network requests, run in thread
        await asyncio.to_thread(site_crawler.initialize)

        # Assuming the structure requires iterating through pages listed on the site.
        home_url = f"{WEBSITE}list/all/all-newstime-0.html" # Example start page
        bot_data['status'] = 'Fetching total page count...'
        logger.info("Fetching initial page to determine page count...")
        soup = await asyncio.to_thread(site_crawler.get_soup, home_url) # Run sync I/O in thread

        # Determine the total number of pages (Example logic, adapt as needed)
        last_page = 0
        page_links = soup.select('.pagination li a[href*="all-newstime-"]')
        if page_links:
            page_numbers = []
            for link in page_links:
                try:
                    # Extract the number part reliably
                    href_parts = link.get('href', '').split('-')
                    if len(href_parts) > 1:
                        num_part = href_parts[-1].split('.')[0]
                        if num_part.isdigit():
                            page_numbers.append(int(num_part))
                except Exception as e:
                    logger.warning(f"Could not parse page number from link {link.get('href')}: {e}")

            if page_numbers:
                last_page = max(page_numbers)
                logger.info(f"Parsed page numbers: {page_numbers}, max: {last_page}")
            else:
                logger.warning("Could not extract any page numbers from pagination links.")
        else:
            logger.warning("Pagination links selector '.pagination li a[href*=\"all-newstime-\"]' not found or empty.")

        total_pages = last_page + 1 # Even if last_page is 0, total_pages will be 1
        logger.info(f"Determined there are {total_pages} pages to crawl (last page number found: {last_page}).")


        # Crawl pages in reverse order (newest first based on URL structure)
        for page_num in range(last_page, -1, -1):
            # Check stop flag at the beginning of each page loop
            if not bot_data.get('crawling', True): # Default to True if somehow missing
                logger.info("Crawl was stopped by user command (checked before processing page).")
                bot_data['status'] = 'Crawl stopped by user.'
                break # Exit the page loop

            status_message = f"Crawling page {page_num + 1} of {total_pages}..."
            bot_data['status'] = status_message
            logger.info(status_message)
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = await asyncio.to_thread(site_crawler.get_soup, page_url) # Run sync I/O in thread

            # Find novel links on the current page (Selector might need adjustment)
            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            logger.info(f"Found {len(novel_links)} novels on page {page_num + 1}.")

            # Process novels on the current page (in reverse order found on page -> oldest first)
            for novel_link in reversed(novel_links):
                # Check stop flag before processing each novel
                if not bot_data.get('crawling', True):
                    logger.info("Stopping novel processing on current page due to stop command.")
                    bot_data['status'] = 'Crawl stopped by user.'
                    break # Exit the novel loop

                novel_url = site_crawler.absolute_url(novel_link['href'])
                novel_title_element = novel_link.select_one('h4.novel-title')
                if novel_title_element:
                    novel_title = novel_title_element.text.strip()
                    logger.debug(f"Queueing processing for: {novel_title} ({novel_url})")
                    bot_data['status'] = f"Processing: {novel_title}" # Update status for current novel

                    # Process each novel asynchronously
                    await process_novel(novel_url, novel_title, context)
                    # Small delay between novels to avoid hammering the site
                    await asyncio.sleep(1.5) # Increased slightly
                else:
                    logger.warning(f"Could not find title for link: {novel_link.get('href')}")

            # Check stop flag again after finishing novels on a page
            if not bot_data.get('crawling', True):
                logger.info("Exiting page loop after processing novels due to stop command.")
                bot_data['status'] = 'Crawl stopped by user.'
                break

    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed critically: {e}"
        if BOT_OWNER:
             await context.bot.send_message(BOT_OWNER, f"Crawl failed critically: {e}")
    finally:
        # Check if the loop finished because it was stopped or naturally
        if bot_data.get('crawling'): # If crawling is still True, it finished naturally
            bot_data['status'] = 'Idle. Crawl finished.'
            logger.info("Crawl cycle finished naturally.")
            if BOT_OWNER:
                await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")
        else: # Status should already reflect 'stopping' or 'stopped'
             # Ensure final status is 'Idle' if stopped
             bot_data['status'] = 'Idle. Crawl stopped.'
             logger.info("Crawl cycle ended because it was stopped.")
             if BOT_OWNER:
                 # Check if the message wasn't already sent by /stop handler potentially
                 # This might send a duplicate, consider refining logic if needed
                 await context.bot.send_message(BOT_OWNER, "Crawl stopped as requested.")

        bot_data['crawling'] = False # Ensure crawling flag is always reset


async def process_novel(novel_url, novel_title, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data
    try:
        # Check database first (cheap operation)
        processed_novel = novels_collection.find_one({"url": novel_url})

        # Instantiate the crawler for this specific novel
        novel_crawler = FanMTLCrawler()
        novel_crawler.novel_url = novel_url # Set the URL
        # Initialization might involve network requests, run in thread
        await asyncio.to_thread(novel_crawler.initialize)

        # Fetch novel info and chapter list (network operation)
        logger.debug(f"Reading novel info for: {novel_title}")
        await asyncio.to_thread(novel_crawler.read_novel_info)
        latest_chapter_count = len(novel_crawler.chapters)
        logger.info(f"'{novel_title}' - Found {latest_chapter_count} chapters on site.")

        if processed_novel:
            stored_chapter_count = processed_novel.get("chapter_count", 0)
            logger.debug(f"'{novel_title}' found in DB with {stored_chapter_count} chapters.")
            # Check if there are new chapters
            if latest_chapter_count > stored_chapter_count:
                status_msg = f"Updating: {novel_title} ({stored_chapter_count} -> {latest_chapter_count})"
                bot_data['status'] = status_msg
                logger.info(status_msg)

                # Send the novel chapters
                if await send_novel(novel_crawler, novel_url, context, caption="Updated"):
                    # Update chapter count only if sending was successful
                    novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"chapter_count": latest_chapter_count, "status": "updated", "last_checked": time.time()}}
                    )
                    logger.info(f"Successfully updated and sent '{novel_title}'.")
                else:
                    logger.warning(f"Failed to send update for '{novel_title}'. Database record not updated with new count.")
                    # Keep status indicating failure for this novel
                    bot_data['status'] = f"Failed sending update: {novel_title}"
            else:
                 logger.info(f"'{novel_title}' is up to date ({latest_chapter_count} chapters). No download needed.")
                 # Optionally update a 'last_checked' timestamp even if no update
                 novels_collection.update_one(
                        {"url": novel_url},
                        {"$set": {"last_checked": time.time(), "status": "checked"}} # Add a status for checked but no update
                    )
                 # Short status update indicating checked
                 bot_data['status'] = f"Checked (up-to-date): {novel_title}"


        else:
            # Novel is new
            status_msg = f"Downloading new: {novel_title} ({latest_chapter_count} chapters)"
            bot_data['status'] = status_msg
            logger.info(status_msg)

            # Send the novel chapters
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
                logger.warning(f"Failed to send new novel '{novel_title}'. Database record not created.")
                # Keep status indicating failure for this novel
                bot_data['status'] = f"Failed sending new: {novel_title}"


    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        bot_data['status'] = f"Error processing: {novel_title}" # Update status on error
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")


async def send_novel(crawler, novel_url, context: ContextTypes.DEFAULT_TYPE, caption=""):
    # Receives the specific crawler instance for this novel
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    app = None # Initialize app to None
    output_path = None # Initialize output_path
    file_path = None # Initialize file_path for use in finally block cleanup
    try:
        # Use a temporary App instance just for packing, passing the crawler
        app = App()
        app.crawler = crawler # Assign the already initialized and info-loaded crawler

        # --- Define Output Path ---
        # Use a temporary directory based on novel title to avoid conflicts
        # Ensure the title is filesystem-safe
        safe_title = "".join(c for c in crawler.novel_title if c.isalnum() or c in (' ', '_')).rstrip()
        output_base_dir = '/tmp/lncrawl_downloads' # Base directory in Render's ephemeral storage
        output_path = os.path.join(output_base_dir, safe_title)
        app.output_path = output_path # Set it on the app instance
        os.makedirs(output_path, exist_ok=True) # Ensure the directory exists
        # ---

        app.pack_as_single_file = True
        app.no_suffix_after_filename = True # Keep filename clean (e.g., Novel Title.epub)

        logger.info(f"Packing '{crawler.novel_title}' to {output_path}...")
        context.application.bot_data['status'] = f"Packing: {crawler.novel_title}"
        # Run the potentially long-running synchronous packing task in a separate thread
        loop = asyncio.get_running_loop()
        # This assumes app.pack_epub() handles the downloading internally
        await loop.run_in_executor(None, app.pack_epub)
        logger.info(f"Finished packing '{crawler.novel_title}'.")

        epub_found = False
        # Check if output_path exists before trying to list its contents
        if os.path.isdir(output_path):
            for filename in os.listdir(output_path):
                if filename.lower().endswith(".epub"): # Use lower() for case-insensitivity
                    file_path = os.path.join(output_path, filename) # Assign file_path here
                    file_size = os.path.getsize(file_path)
                    logger.info(f"EPUB found: {file_path} (Size: {file_size / 1024:.2f} KB). Attempting to send...")
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
                                    connect_timeout=60,
                                    pool_timeout=300 # Add pool timeout
                                )
                            logger.info(f"Successfully sent {filename}")
                            epub_found = True
                        except Exception as send_err:
                             logger.error(f"Failed to send document {filename}: {send_err}", exc_info=True)
                             context.application.bot_data['status'] = f"Failed sending: {filename}"
                             if BOT_OWNER:
                                try: # Try sending error message separately
                                    await context.bot.send_message(BOT_OWNER, f"Failed to send {filename} for '{crawler.novel_title}': {send_err}")
                                except Exception as report_err:
                                     logger.error(f"Failed to report send error to user: {report_err}")
                             # Failure to send means overall failure for this novel attempt
                             epub_found = False # Explicitly set to false on error

                    else:
                        # If no BOT_OWNER, log that we would have sent it
                        logger.info(f"BOT_OWNER not set. Would have sent: {filename}")
                        epub_found = True # Consider it 'found' for logic purposes

                    break # Send only the first epub found for this novel

            # Check if loop completed without finding an epub
            if not epub_found and os.path.isdir(output_path): # Check again
                 logger.warning(f"No EPUB file found in {output_path} for novel '{crawler.novel_title}' ({novel_url})")
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
            await context.bot.send_message(BOT_OWNER, f"Error packing or sending '{novel_name}': {e}")
        return False
    finally:
        # Clean up the specific file if it exists
        if file_path and os.path.exists(file_path):
             try:
                 os.remove(file_path)
                 logger.info(f"Removed temporary file: {file_path}")
             except OSError as rm_err:
                 logger.warning(f"Could not remove temporary file {file_path}: {rm_err}")
        # Clean up the entire output directory for this novel if it exists
        if output_path and os.path.isdir(output_path):
            try:
                shutil.rmtree(output_path)
                logger.info(f"Cleaned up output directory: {output_path}")
            except Exception as clean_err:
                logger.warning(f"Could not clean up output directory {output_path}: {clean_err}")


# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # <<< ADDED LOGGING >>>
    logger.info(f"Received update in start_command: {update}") # Log the full update object
    logger.info(f"Entered start_command handler for update ID: {update.update_id}")
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"/start command user ID: {user_id} | BOT_OWNER: {BOT_OWNER}") # Log comparison values
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
        thread = Thread(target=crawl_and_send_sync, args=(context,), name="CrawlThread", daemon=True)
        thread.start()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     # <<< ADDED LOGGING >>>
     logger.info(f"Received update in status_command: {update}") # Log the full update object
     logger.info(f"Entered status_command handler for update ID: {update.update_id}")
     user_id = update.effective_user.id if update.effective_user else None
     logger.info(f"/status command user ID: {user_id} | BOT_OWNER: {BOT_OWNER}") # Log comparison values
     # Log status requests as debug to reduce noise unless needed
     logger.debug(f"Received /status command from user ID: {user_id}")
     if not user_id or user_id != BOT_OWNER:
        logger.warning(f"Unauthorized /status attempt by user ID: {user_id}")
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return
     current_status = context.application.bot_data.get('status', 'Idle.')
     is_crawling = context.application.bot_data.get('crawling', False)
     await update.message.reply_text(f"**Crawling:** {'Yes' if is_crawling else 'No'}\n**Status:** {current_status}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     # <<< ADDED LOGGING >>>
     logger.info(f"Received update in stop_command: {update}") # Log the full update object
     logger.info(f"Entered stop_command handler for update ID: {update.update_id}")
     user_id = update.effective_user.id if update.effective_user else None
     logger.info(f"/stop command user ID: {user_id} | BOT_OWNER: {BOT_OWNER}") # Log comparison values
     logger.info(f"Received /stop command from user ID: {user_id}")
     if not user_id or user_id != BOT_OWNER:
        logger.warning(f"Unauthorized /stop attempt by user ID: {user_id}")
        await update.message.reply_text("Sorry, only the bot owner can use this command.")
        return

     bot_data = context.application.bot_data
     if bot_data.get('crawling'):
        logger.info("Setting crawling flag to False due to /stop command.")
        bot_data['crawling'] = False # Set flag to prevent starting new tasks
        bot_data['status'] = 'Stopping crawl...' # Update status immediately
        await update.message.reply_text("Stopping crawl... I will finish the current operation (e.g., novel download/send) and then stop.")
     else:
        logger.info("Received /stop command but not currently crawling.")
        # Ensure status is Idle if stop is pressed when not crawling
        bot_data['status'] = 'Idle. Not currently crawling.'
        bot_data['crawling'] = False # Ensure flag is false
        await update.message.reply_text("I am not currently crawling.")

# --- Initialization and Startup ---
def run_bot_polling(application: Application):
    """Runs the Telegram bot's polling loop in the current thread."""
    # <<< MODIFIED LOGGING >>>
    thread_name = current_thread().name # Get current thread name
    logger.info(f"Starting Telegram bot polling in thread: {thread_name}")
    try:
        # --- Create and set event loop for *this* thread ---
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("Created and set new event loop for bot polling thread.")

        # --- FIX: Explicitly assign the created loop to the application instance ---
        application.loop = loop
        logger.info(f"Assigned loop {loop} to application instance.")
        # --- End Fix ---

        # Disable PTB's signal handlers as Render/Docker manages signals
        logger.info("Calling application.run_polling...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            stop_signals=None # Pass None to disable signal handling
        )
        # <<< ADDED LOGGING >>>
        logger.warning("application.run_polling finished unexpectedly.") # Should ideally run forever

    except Exception as e:
        logger.critical(f"Bot polling loop failed critically: {e}", exc_info=True)
        # Force exit the entire process if polling fails, Gunicorn should restart it.
        os._exit(1)
    finally:
        # <<< ADDED LOGGING >>>
        logger.critical("run_bot_polling function is exiting.") # Should not happen unless there's an error


#
# --- STARTUP LOGIC ---
# This code runs when Gunicorn imports the file (i.e., when the worker starts).
#

# Validate Environment Variables
if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER_STR]):
     logger.fatal("FATAL: Missing one or more environment variables (BOT_TOKEN, MONGO_URI, BOT_OWNER). Bot cannot start.")
     # Raising RuntimeError here will stop Gunicorn worker from booting
     raise RuntimeError("Missing essential environment variables.")

# Convert BOT_OWNER to int *after* checking it exists
try:
    BOT_OWNER = int(BOT_OWNER_STR)
    logger.info(f"Bot owner ID set to: {BOT_OWNER}")
except (ValueError, TypeError):
    logger.fatal("FATAL: BOT_OWNER environment variable is not a valid integer.")
    raise RuntimeError("BOT_OWNER must be a valid integer.")


if not APP_URL:
    logger.warning("APP_URL environment variable is not set. Self-pinging will be disabled.")
else:
    # Ensure APP_URL has http/https prefix
    if not APP_URL.startswith(('http://', 'https://')):
        logger.warning(f"APP_URL '{APP_URL}' might be missing http/https prefix. Assuming https.")
        APP_URL = f"https://{APP_URL}"
    logger.info(f"APP_URL set to: {APP_URL}")


# Create the Telegram Application
logger.info("Building Telegram Application...")
# Consider adding connection pool size if experiencing timeout errors during high load
# telegram_app = Application.builder().token(BOT_TOKEN).pool_timeout(300).connect_timeout(60).read_timeout(180).write_timeout(180).build()
telegram_app = Application.builder().token(BOT_TOKEN).build()
logger.info("Telegram Application built.")

# Initialize bot state in bot_data
telegram_app.bot_data['status'] = 'Idle. Ready to start.'
telegram_app.bot_data['crawling'] = False

# --- Register command handlers using MessageHandler ---
logger.info("Registering command handlers...")
# <<< USING MessageHandler with filters >>>
telegram_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/start(?:@\w+)?$'), start_command))
telegram_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/status(?:@\w+)?$'), status_command))
telegram_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/stop(?:@\w+)?$'), stop_command))
# --- End change ---
logger.info("Command handlers registered.")


# Start self-pinging in a background thread ONLY if APP_URL is set
if APP_URL:
    ping_thread = Thread(target=self_ping, name="SelfPingThread", daemon=True)
    ping_thread.start()
    logger.info("Self-pinging keep-alive service started.")
else:
    logger.info("Self-pinging disabled as APP_URL is not set.")

# Start the bot polling in a separate background thread
# Pass the created telegram_app instance to the thread function
logger.info("Starting bot polling thread...")
# Explicitly name the thread for easier debugging
bot_thread = Thread(target=run_bot_polling, args=(telegram_app,), name="TelegramPollingThread", daemon=True)
bot_thread.start()

# Log that the main thread (Gunicorn worker) is now ready
logger.info("Bot polling thread started. Gunicorn worker initialized and serving Flask app.")

# Gunicorn manages the main process life cycle and serves `server_app`.
# The bot polling and pinging run in background daemon threads.
# No `if __name__ == "__main__":` block or infinite loop needed here.
