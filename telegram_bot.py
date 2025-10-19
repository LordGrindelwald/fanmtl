import os
import logging
import time
import requests
import asyncio
from threading import Thread, current_thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import shutil
import re
from datetime import datetime
import sys
import html
import json
import traceback
from telegram.constants import ParseMode

# --- DIRECTLY MODIFY PYTHON PATH ---
# Add the '/app' directory (where lncrawl lives) to Python's search path
app_path = '/app'
if app_path not in sys.path:
    sys.path.insert(0, app_path)
# --- END PATH MODIFICATION ---

# Import necessary lncrawl components directly
from lncrawl.core.app import App
from lncrawl.sources.en.f.fanmtl import FanMTLCrawler

from pymongo import MongoClient, errors

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
BOT_OWNER_STR = os.getenv("BOT_OWNER")
WEBSITE = "https://fanmtl.com/"
APP_URL = os.getenv("APP_URL")

# --- Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s [%(threadName)s] - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
server_app = Flask(__name__)

BOT_OWNER = None

# --- Database Connection ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_database("novel_bot")
    novels_collection = db.novels
    client.admin.command('ping')
    logger.info("Successfully connected to MongoDB.")
except errors.ConnectionFailure as e:
    logger.fatal(f"Could not connect to MongoDB: {e}")
    raise RuntimeError(f"Could not connect to MongoDB: {e}")
except Exception as e:
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
                ping_url = f"{APP_URL.rstrip('/')}/health"
                requests.get(ping_url, timeout=10)
                logger.debug(f"Sent keep-alive ping to self: {ping_url}")
            else:
                if not hasattr(self_ping, "logged_missing_url"):
                    logger.warning("APP_URL not set, self-pinging disabled.")
                    self_ping.logged_missing_url = True
                time.sleep(14 * 60)
                continue
        except requests.exceptions.RequestException as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in self_ping: {e}", exc_info=True)

        time.sleep(14 * 60)

# --- Error Handler Function ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    if BOT_OWNER:
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


# --- Core Bot Logic ---
def crawl_and_send_sync(context: ContextTypes.DEFAULT_TYPE, loop: asyncio.AbstractEventLoop):
    """
    Synchronous wrapper to run the async crawl in a new thread.
    The event loop is passed from the command handler.
    """
    if not context.application:
        logger.error("Telegram application not found in context.")
        return

    try:
        # Use the loop passed from start_command
        if not (loop and loop.is_running()):
             raise RuntimeError("Event loop passed from start_command is not valid or not running.")
        logger.info(f"Using event loop passed from start_command: {loop} for crawl task.")

    except Exception as e:
        logger.error(f"Could not retrieve a running event loop: {e}", exc_info=True)
        if BOT_OWNER:
             try:
                 # We should have a valid loop to report this error
                 asyncio.run_coroutine_threadsafe(
                     context.bot.send_message(BOT_OWNER, f"Error starting crawl: Invalid event loop. {e}"),
                     loop
                 ).result(timeout=10)
             except Exception as report_e:
                 logger.error(f"Failed to report loop error to user (this is bad): {report_e}")
        return

    logger.info(f"Scheduling crawl_and_send coroutine on loop {loop}")
    asyncio.run_coroutine_threadsafe(crawl_and_send(context), loop)


async def crawl_and_send(context: ContextTypes.DEFAULT_TYPE):
    global BOT_OWNER
    bot_data = context.application.bot_data

    if bot_data.get('crawling'):
        logger.warning("Crawl task requested but already running. Ignoring.")
        return

    bot_data['crawling'] = True
    bot_data['status'] = 'Initializing crawl...'
    logger.info("Starting a new crawl cycle...")

    try:
        site_crawler = FanMTLCrawler()
        await asyncio.to_thread(site_crawler.initialize)

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
                    await process_novel(novel_url, novel_title, context)
                    await asyncio.sleep(1.5)
                else:
                    logger.warning(f"Could not find title for link: {novel_link.get('href')}")

            if not bot_data.get('crawling', True):
                break

    except Exception as e:
        logger.error(f"A critical error occurred during the crawl: {e}", exc_info=True)
        bot_data['status'] = f"Crawl failed critically: {e}"
        if BOT_OWNER:
             await context.bot.send_message(BOT_OWNER, f"Crawl failed critically: {e}")
    finally:
        if bot_data.get('crawling'):
            bot_data['status'] = 'Idle. Crawl finished.'
            logger.info("Crawl cycle finished naturally.")
            if BOT_OWNER:
                await context.bot.send_message(BOT_OWNER, "Finished crawling all pages.")
        else:
             bot_data['status'] = 'Idle. Crawl stopped.'
             logger.info("Crawl cycle ended because it was stopped.")
             if BOT_OWNER:
                 await context.bot.send_message(BOT_OWNER, "Crawl stopped as requested.")

        bot_data['crawling'] = False


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
                        "status": "processed"
                    },
                    "$setOnInsert": {
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
    epub_path = None
    try:
        app = App()
        app.crawler = crawler
        safe_title = "".join(c for c in crawler.novel_title if c.isalnum() or c in (' ', '_')).rstrip()
        output_base_dir = '/tmp/lncrawl_downloads'
        output_path = os.path.join(output_base_dir, safe_title)
        app.output_path = output_path
        os.makedirs(output_path, exist_ok=True)

        app.pack_as_single_file = True
        app.no_suffix_after_filename = True

        logger.info(f"Packing '{crawler.novel_title}' to {output_path}...")
        context.application.bot_data['status'] = f"Packing: {crawler.novel_title}"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, app.pack_epub)
        logger.info(f"Finished packing '{crawler.novel_title}'.")

        epub_found = False
        if os.path.isdir(output_path):
            for filename in os.listdir(output_path):
                if filename.lower().endswith(".epub"):
                    epub_path = os.path.join(output_path, filename)
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
                             epub_found = False
                    else:
                        logger.info(f"BOT_OWNER not set. Would have sent: {filename}")
                        epub_found = True
                    break

            if not epub_found:
                 logger.warning(f"No EPUB file found in {output_path} after packing '{crawler.novel_title}'.")
                 context.application.bot_data['status'] = f"No EPUB for: {crawler.novel_title}"
        else:
             logger.error(f"Output path '{output_path}' disappeared or wasn't created for '{crawler.novel_title}'.")
             context.application.bot_data['status'] = f"Output path error for: {crawler.novel_title}"
        return epub_found

    except Exception as e:
        novel_name = crawler.novel_title if crawler and hasattr(crawler, 'novel_title') else "Unknown Novel"
        logger.error(f"Failed during pack/send process for '{novel_name}': {e}", exc_info=True)
        context.application.bot_data['status'] = f"Error packing/sending: {novel_name}"
        if BOT_OWNER:
            await context.bot.send_message(BOT_OWNER, f"Error packing or sending '{novel_name}': {e}")
        return False
    finally:
        if epub_path and os.path.exists(epub_path):
             try:
                 os.remove(epub_path)
                 logger.info(f"Removed temporary file: {epub_path}")
             except OSError as rm_err:
                 logger.warning(f"Could not remove temporary file {epub_path}: {rm_err}")
        if output_path and os.path.isdir(output_path):
            try:
                shutil.rmtree(output_path)
                logger.info(f"Cleaned up output directory tree: {output_path}")
            except Exception as clean_err:
                logger.warning(f"Could not clean up output directory {output_path}: {clean_err}")


# --- Telegram Command Handlers (Updated) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    logger.info("Executing /start command for owner.")
    if context.application.bot_data.get('crawling'):
        await update.message.reply_text("A crawl is already in progress. Use /status to check.")
    else:
        await update.message.reply_text("Crawl started. I will process all novels. Use /status to check my progress.")
        
        # --- FIX ---
        # Get the event loop *here* while we are in the main async thread
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as e:
            logger.error(f"Could not get running loop in start_command: {e}", exc_info=True)
            await update.message.reply_text(f"Error: Could not get event loop to start crawl: {e}")
            return
        
        # Pass both the context and the loop to the sync wrapper in the new thread
        thread = Thread(target=crawl_and_send_sync, args=(context, loop), name="CrawlThread", daemon=True)
        thread.start()
        # --- END FIX ---

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /status command."""
    logger.info("Executing /status command for owner.")
    current_status = context.application.bot_data.get('status', 'Idle.')
    is_crawling = context.application.bot_data.get('crawling', False)
    await update.message.reply_text(f"**Crawling:** {'Yes' if is_crawling else 'No'}\n**Status:** {current_status}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /stop command."""
    logger.info("Executing /stop command for owner.")
    bot_data = context.application.bot_data
    if bot_data.get('crawling'):
        bot_data['crawling'] = False
        bot_data['status'] = 'Stopping crawl...'
        await update.message.reply_text("Stopping crawl... I will finish the current operation and then stop.")
    else:
        bot_data['status'] = 'Idle. Not currently crawling.'
        bot_data['crawling'] = False
        await update.message.reply_text("I am not currently crawling.")

async def unauthorized_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles commands from users who are not the owner."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    logger.warning(f"Unauthorized command attempt by user ID: {user_id}")
    await update.message.reply_text("Sorry, only the bot owner can use this bot's commands.")


# --- Initialization and Startup ---
def initialize_app() -> Application:
    global BOT_OWNER, APP_URL

    if not all([BOT_TOKEN, MONGO_URI, BOT_OWNER_STR]):
         logger.fatal("FATAL: Missing one or more environment variables (BOT_TOKEN, MONGO_URI, BOT_OWNER). Bot cannot start.")
         raise RuntimeError("Missing essential environment variables.")

    try:
        BOT_OWNER = int(BOT_OWNER_STR)
        logger.info(f"Bot owner ID set to: {BOT_OWNER}")
    except (ValueError, TypeError):
        logger.fatal("FATAL: BOT_OWNER environment variable is not a valid integer.")
        raise RuntimeError("BOT_OWNER must be a valid integer.")

    if not APP_URL:
        logger.warning("APP_URL environment variable is not set. Self-pinging will be disabled.")
    else:
        if not APP_URL.startswith(('http://', 'https://')):
            logger.warning(f"APP_URL '{APP_URL}' might be missing http/https prefix. Assuming https.")
            APP_URL = f"https://{APP_URL}"
        logger.info(f"APP_URL set to: {APP_URL}")

    logger.info("Building Telegram Application...")
    application = Application.builder().token(BOT_TOKEN).build()
    logger.info("Telegram Application built.")

    application.bot_data['status'] = 'Idle. Ready to start.'
    application.bot_data['crawling'] = False

    # --- Register handlers ---
    logger.info("Registering command and error handlers...")
    owner_filter = filters.User(user_id=BOT_OWNER)

    application.add_handler(CommandHandler("start", start_command, filters=owner_filter))
    application.add_handler(CommandHandler("status", status_command, filters=owner_filter))
    application.add_handler(CommandHandler("stop", stop_command, filters=owner_filter))

    application.add_handler(MessageHandler(filters.COMMAND & ~owner_filter, unauthorized_user_handler))

    application.add_error_handler(error_handler)
    logger.info("Command and error handlers registered.")
    
    return application

# No code is run here when imported
