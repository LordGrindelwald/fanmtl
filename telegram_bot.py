import os
import logging
import time
import requests
import asyncio
from threading import Thread, current_thread
from flask import Flask
from telegram import Update
# Need filters for error handler, keep CommandHandler for commands
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import shutil
import re
from datetime import datetime
import sys
import html # For error handler
import json # For error handler
import traceback # For error handler
from telegram.constants import ParseMode # For error handler

# --- DIRECTLY MODIFY PYTHON PATH ---
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
    level=logging.INFO # Keep level at INFO
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
    client.admin.command('ping') # Use ping for connection check
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.fatal(f"Could not connect to MongoDB: {e}")
    # Raise error to stop Gunicorn from trying to run
    raise RuntimeError(f"Could not connect to MongoDB: {e}")
except Exception as e: # Catch other potential errors like auth errors
    logger.fatal(f"An error occurred during MongoDB connection: {e}")
    raise RuntimeError(f"An error occurred during MongoDB connection: {e}")


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
                # logger.info(f"Sent keep-alive ping to self: {ping_url}") # Make this debug level
                logger.debug(f"Sent keep-alive ping to self: {ping_url}")
            else:
                # Log only once if APP_URL is not set, then sleep
                if not hasattr(self_ping, "logged_missing_url"):
                    logger.warning("APP_URL not set, self-pinging disabled.")
                    self_ping.logged_missing_url = True # Prevent repeated logging
                time.sleep(14 * 60) # Still sleep
                continue # Skip the rest of the loop
        except requests.exceptions.RequestException as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in self_ping: {e}", exc_info=True)

        # Sleep regardless of success or failure
        time.sleep(14 * 60) # Sleep for 14 minutes

# --- Error Handler Function ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        # commenting out chat/user data for brevity unless needed
        # f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        # f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    # Finally, send the message
    if BOT_OWNER:
        # Split the message if it's too long for Telegram
        MAX_MESSAGE_LENGTH = 4096
        try:
            for i in range(0, len(message), MAX_MESSAGE_LENGTH):
                await context.bot.send_message(
                    chat_id=BOT_OWNER, text=message[i:i + MAX_MESSAGE_LENGTH], parse_mode=ParseMode.HTML
                )
        except Exception as send_err:
             logger.error(f"Failed to send error traceback to owner: {send_err}")
    else:
        logger.error("BOT_OWNER not set, cannot send error message via Telegram.")


# --- Core Bot Logic (Runs in a separate thread) ---
def crawl_and_send_sync(context: ContextTypes.DEFAULT_TYPE):
    """Synchronous wrapper to run the async crawl process in a thread."""
    global telegram_app # Need the global app instance
    if not telegram_app:
        logger.error("Telegram application not initialized.")
        return

    # Try accessing the loop explicitly set on the app instance
    try:
        if hasattr(telegram_app, 'loop') and telegram_app.loop:
            loop = telegram_app.loop
            if not loop.is_running():
                 raise RuntimeError("Event loop found but is not running.")
            logger.info(f"Using event loop from telegram_app.loop: {loop} for crawl task.")
        else:
             raise RuntimeError("telegram_app.loop attribute not found or is None.")

    except Exception as e:
        logger.error(f"Could not retrieve a running event loop for crawl_and_send: {e}", exc_info=True)
        # Attempt to inform the user if possible
        if BOT_OWNER:
             try:
                 # Use run_coroutine_threadsafe if we're in a different thread trying to use the bot's loop
                 asyncio.run_coroutine_threadsafe(
                     context.bot.send_message(BOT_OWNER, f"Error starting crawl: Could not get event loop. {e}"),
                     telegram_app.loop # Use the intended loop
                 ).result(timeout=10) # Add timeout to prevent blocking indefinitely
             except Exception as report_e:
                 logger.error(f"Failed to report loop error to user: {report_e}")
        return

    # Schedule the async function to run on the retrieved loop
    logger.info(f"Scheduling crawl_and_send coroutine on loop {loop}")
    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)


async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER # Ensure BOT_OWNER is accessible
    bot_data = context.application.bot_data

    # Check if already crawling (add re-entrancy protection)
    if bot_data.get('crawling'):
        logger.warning("Crawl task requested but already running. Ignoring.")
        # Optionally notify user if needed, but might be noisy
        # await context.bot.send_message(BOT_OWNER, "Crawl requested, but one is already in progress.")
        return

    # Set crawling flag immediately
    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        # Instantiate the specific crawler directly
        site_crawler = FanMTLCrawler()
        # Initialization might involve network requests, run in thread
        await asyncio.to_thread(site_crawler.initialize)

        # Determine the total number of pages
        home_url = f"{WEBSITE}list/all/all-newstime-0.html"
        bot_data['status'] = 'Fetching total page count...'
        logger.info("Fetching initial page to determine page count...")
        soup = await asyncio.to_thread(site_crawler.get_soup, home_url)

        last_page = 0
        page_links = soup.select('.pagination li a[href*="all-newstime-"]')
        if page_links:
            page_numbers = []
            for link in page_links:
                try:
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
                logger.warning("Could not extract any page numbers from pagination.")
        else:
            logger.warning("Pagination links not found.")

        total_pages = last_page + 1
        logger.info(f"Determined there are {total_pages} pages to crawl.")

        # Crawl pages in reverse order
        for page_num in range(last_page, -1, -1):
            if not bot_data.get('crawling', True):
                logger.info("Crawl stopped by user command (checked before page).")
                break

            status_message = f"Crawling page {page_num + 1} of {total_pages}..."
            bot_data['status'] = status_message
            logger.info(status_message)
            page_url = f"{WEBSITE}list/all/all-newstime-{page_num}.html"
            page_soup = await asyncio.to_thread(site_crawler.get_soup, page_url)

            novel_links = page_soup.select('ul.novel-list li.novel-item a')
            logger.info(f"Found {len(novel_links)} novels on page {page_num + 1}.")

            for novel_link in reversed(novel_links):
                if not bot_data.get('crawling', True):
                    logger.info("Stopping novel processing due to stop command.")
                    break

                novel_url = site_crawler.absolute_url(novel_link['href'])
                novel_title_element = novel_link.select_one('h4.novel-title')
                if novel_title_element:
                    novel_title = novel_title_element.text.strip()
                    logger.debug(f"Queueing processing for: {novel_title}")
                    bot_data['status'] = f"Processing: {novel_title}"

                    # Process each novel
                    await process_novel(novel_url, novel_title, context)
                    await asyncio.sleep(1.5) # Delay
                else:
                    logger.warning(f"Could not find title for link: {novel_link.get('href')}")

            if not bot_data.get('crawling', True):
                break # Exit outer loop if inner loop broke due to stop

    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed critically: {e}"
        if BOT_OWNER:
             await context.bot.send_message(BOT_OWNER, f"Crawl failed critically: {e}")
    finally:
        # Final status update logic
        if bot_data.get('crawling'): # Finished naturally
            bot_data['status'] = 'Idle. Crawl finished.'
            logger.info("Crawl cycle finished naturally.")
            if BOT_OWNER:
                await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")
        else: # Was stopped
             bot_data['status'] = 'Idle. Crawl stopped.'
             logger.info("Crawl cycle ended because it was stopped.")
             if BOT_OWNER:
                 await context.bot.send_message(BOT_OWNER, "Crawl stopped as requested.")

        bot_data['crawling'] = False # Ensure flag is reset


async def process_novel(novel_url, novel_title, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER
    bot_data = context.application.bot_data
    logger.info(f"Processing novel: {novel_title} ({novel_url})")
    try:
        processed_novel = novels_collection.find_one({"url": novel_url})

        novel_crawler = FanMTLCrawler()
        novel_crawler.novel_url = novel_url
        await asyncio.to_thread(novel_crawler.initialize)
        await asyncio.to_thread(novel_crawler.read_novel_info)
        latest_chapter_count = len(novel_crawler.chapters)
        logger.info(f"'{novel_title}' - Found {latest_chapter_count} chapters on site.")

        stored_chapter_count = 0
        is_update = False
        if processed_novel:
            stored_chapter_count = processed_novel.get("chapter_count", 0)
            is_update = True
            logger.debug(f"'{novel_title}' found in DB with {stored_chapter_count} chapters.")

        if not is_update or latest_chapter_count > stored_chapter_count:
            status_prefix = "Updating" if is_update else "Downloading new"
            count_info = f"({stored_chapter_count} -> {latest_chapter_count})" if is_update else f"({latest_chapter_count} chapters)"
            status_msg = f"{status_prefix}: {novel_title} {count_info}"
            bot_data['status'] = status_msg
            logger.info(status_msg)

            caption = "Updated" if is_update else "New"
            if await send_novel(novel_crawler, context, caption):
                update_data = {
                    "$set": {
                        "chapter_count": latest_chapter_count,
                        "last_checked": time.time(),
                        "status": "processed" # or 'updated'
                    },
                    "$setOnInsert": { # Only set these fields if it's a new document
                         "url": novel_url,
                         "title": novel_title,
                         "first_added": time.time(),
                    }
                }
                novels_collection.update_one({"url": novel_url}, update_data, upsert=True)
                logger.info(f"Successfully processed and sent '{novel_title}'.")
            else:
                logger.warning(f"Failed to send {caption.lower()} novel '{novel_title}'. Database not updated.")
                bot_data['status'] = f"Failed sending {caption.lower()}: {novel_title}"
        else:
            logger.info(f"'{novel_title}' is up to date ({latest_chapter_count} chapters). No download needed.")
            novels_collection.update_one(
                {"url": novel_url},
                {"$set": {"last_checked": time.time(), "status": "checked"}}
            )
            bot_data['status'] = f"Checked (up-to-date): {novel_title}"

    except Exception as e:
        logger.error(f"Failed to process novel '{novel_title}' ({novel_url}): {e}", exc_info=True)
        bot_data['status'] = f"Error processing: {novel_title}"
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error processing '{novel_title}': {e}")


async def send_novel(crawler: FanMTLCrawler, context: ContextTypes.DEFAULT_TYPE, caption="") -> bool:
    global BOT_OWNER
    app = None
    output_path = None
    epub_path = None # Define epub_path to be accessible in finally
    try:
        app = App()
        app.crawler = crawler
        # Ensure the crawler has necessary app reference if needed by pack_epub
        # This depends on lncrawl's internal structure; might be app.crawler.app = app
        # For safety, let's assume the App() instance needs the main app context if available
        # if hasattr(crawler, 'app'): crawler.app = app # Check before assigning


        safe_title = "".join(c for c in crawler.novel_title if c.isalnum() or c in (' ', '_')).rstrip()
        output_base_dir = '/tmp/lncrawl_downloads'
        output_path = os.path.join(output_base_dir, safe_title)
        app.output_path = output_path # Crucial: Set output path *before* packing
        os.makedirs(output_path, exist_ok=True)

        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        logger.info(f"Packing '{crawler.novel_title}' to {output_path}...")
        context.application.bot_data['status'] = f"Packing: {crawler.novel_title}"

        # Run synchronous packing in executor
        loop = asyncio.get_running_loop()
        # pack_epub itself should handle finding the correct binder and calling bind()
        await loop.run_in_executor(None, app.pack_epub)
        logger.info(f"Finished packing '{crawler.novel_title}'.")

        # Find the created EPUB file
        epub_found = False
        if os.path.isdir(output_path):
            for filename in os.listdir(output_path):
                if filename.lower().endswith(".epub"):
                    epub_path = os.path.join(output_path, filename) # Assign epub_path
                    file_size = os.path.getsize(epub_path)
                    logger.info(f"EPUB found: {epub_path} (Size: {file_size / 1024:.2f} KB).")
                    context.application.bot_data['status'] = f"Sending: {filename}"

                    if BOT_OWNER:
                        logger.info(f"Attempting to send {filename} to owner {BOT_OWNER}...")
                        try:
                            with open(epub_path, "rb") as f:
                                await context.bot.send_document(
                                    chat_id=BOT_OWNER, document=f, filename=filename,
                                    caption=f"{caption}: {crawler.novel_title}".strip(),
                                    read_timeout=300, write_timeout=300, connect_timeout=60, pool_timeout=300
                                )
                            logger.info(f"Successfully sent {filename}")
                            epub_found = True
                        except Exception as send_err:
                             logger.error(f"Failed to send document {filename}: {send_err}", exc_info=True)
                             context.application.bot_data['status'] = f"Failed sending: {filename}"
                             if BOT_OWNER:
                                try:
                                    await context.bot.send_message(BOT_OWNER, f"Failed to send {filename} for '{crawler.novel_title}': {send_err}")
                                except Exception as report_err:
                                     logger.error(f"Failed to report send error to user: {report_err}")
                             epub_found = False # Explicitly set to false on error
                    else:
                        logger.info(f"BOT_OWNER not set. Would have sent: {filename}")
                        epub_found = True

                    break # Stop after finding the first EPUB

            if not epub_found:
                 logger.warning(f"No EPUB file found in {output_path} after packing '{crawler.novel_title}'.")
                 context.application.bot_data['status'] = f"No EPUB for: {crawler.novel_title}"
        else:
             logger.error(f"Output path '{output_path}' disappeared or wasn't created for '{crawler.novel_title}'.")
             context.application.bot_data['status'] = f"Output path error for: {crawler.novel_title}"

        return epub_found

    except Exception as e:
        novel_name = crawler.novel_title if crawler and hasattr(crawler, 'novel_title') else novel_url
        logger.error(f"Failed during pack/send process for '{novel_name}': {e}", exc_info=True)
        context.application.bot_data['status'] = f"Error packing/sending: {novel_name}"
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error packing or sending '{novel_name}': {e}")
        return False
    finally:
        # Clean up the specific EPUB file first if it exists
        if epub_path and os.path.exists(epub_path):
             try:
                 os.remove(epub_path)
                 logger.info(f"Removed temporary file: {epub_path}")
             except OSError as rm_err:
                 logger.warning(f"Could not remove temporary file {epub_path}: {rm_err}")
        # Clean up the entire output directory for this novel if it exists and is empty or only had the epub
        if output_path and os.path.isdir(output_path):
            try:
                # Check if dir is empty after removing epub, then remove dir
                if not os.listdir(output_path):
                     os.rmdir(output_path)
                     logger.info(f"Cleaned up empty output directory: {output_path}")
                else: # Or just remove the whole tree if cleanup is desired regardless
                     shutil.rmtree(output_path)
                     logger.info(f"Cleaned up output directory tree: {output_path}")

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
     logger.debug(f"Received /status command from user ID: {user_id}") # Changed level to DEBUG
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
     # logger.info(f"Received /stop command from user ID: {user_id}") # Duplicate log removed
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
    thread_name = current_thread().name # Get current thread name
    logger.info(f"Starting Telegram bot polling in thread: {thread_name}")
    try:
        # Create and set event loop for *this* thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info(f"Created and set new event loop {loop} for bot polling thread.")

        # Explicitly assign the created loop to the application instance
        application.loop = loop
        logger.info(f"Assigned loop {loop} to application instance.")

        # Disable PTB's signal handlers as Render/Docker manages signals
        logger.info("Calling application.run_polling...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            stop_signals=None # Pass None to disable signal handling
        )
        logger.warning("application.run_polling finished unexpectedly.") # Should ideally run forever

    except Exception as e:
        logger.critical(f"Bot polling loop failed critically: {e}", exc_info=True)
        # Force exit the entire process if polling fails, Gunicorn should restart it.
        os._exit(1) # Use os._exit in threads to terminate the process
    finally:
        logger.critical("run_bot_polling function is exiting.") # Should not normally happen


#
# --- STARTUP LOGIC ---
# Wrap startup in a function to avoid potential global scope issues with Gunicorn workers
#
def initialize_app():
    global telegram_app, BOT_OWNER, APP_URL # Declare globals we modify

    # Validate Environment Variables
    if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER_STR]):
         logger.fatal("FATAL: Missing one or more environment variables (BOT_TOKEN, MONGO_URI, BOT_OWNER). Bot cannot start.")
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
            APP_URL = f"https://{APP_URL}" # Modify the global
        logger.info(f"APP_URL set to: {APP_URL}")


    # Create the Telegram Application
    logger.info("Building Telegram Application...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    logger.info("Telegram Application built.")

    # Initialize bot state in bot_data
    telegram_app.bot_data['status'] = 'Idle. Ready to start.'
    telegram_app.bot_data['crawling'] = False

    # --- Register handlers ---
    logger.info("Registering command and error handlers...")
    # <<< REVERTED TO CommandHandler >>>
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))

    # Add the error handler (must be last or use group parameter)
    telegram_app.add_error_handler(error_handler) # Use add_error_handler for PTB v20+
    # <<< END REVERT >>>
    logger.info("Command and error handlers registered.")


    # Start self-pinging in a background thread ONLY if APP_URL is set
    if APP_URL:
        ping_thread = Thread(target=self_ping, name="SelfPingThread", daemon=True)
        ping_thread.start()
        logger.info("Self-pinging keep-alive service started.")
    else:
        logger.info("Self-pinging disabled as APP_URL is not set.")

    # Start the bot polling in a separate background thread
    logger.info("Starting bot polling thread...")
    bot_thread = Thread(target=run_bot_polling, args=(telegram_app,), name="TelegramPollingThread", daemon=True)
    bot_thread.start()

    logger.info("Bot polling thread started. Gunicorn worker initialization complete.")

# --- Run Initialization ---
# Check if running under Gunicorn (or similar WSGI server) which imports the module
# or if run directly (e.g., python telegram_bot.py) for local testing
if __name__ != "__main__":
    # Gunicorn imports the module, so call initialize_app() here
    initialize_app()
# else:
    # If you wanted to run locally without Gunicorn, you'd call initialize_app()
    # and maybe add a simple server run or just keep the script alive.
    # For Render/Gunicorn, this 'else' block isn't strictly needed.
    # logger.info("Running locally without Gunicorn.")
    # initialize_app()
    # server_app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080))) # Example for local Flask run
    pass
