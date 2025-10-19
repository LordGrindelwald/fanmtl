# -*- coding: utf-8 -*-
"""
Bind chapters into EPUB
"""
import concurrent.futures
import logging
import os
import shutil
from typing import Dict, List, Tuple

import ebooklib
from ebooklib import epub
from tqdm.auto import tqdm

from ...assets.epub import (
    # Assume these template files exist in lncrawl/assets/epub/
    chapter_xhtml_template,
    content_opf_template, # You might need to manually define this if not easily importable
    cover_xhtml_template,
    nav_xhtml_template, # You might need to manually define this if not easily importable
    style_css_template,
    toc_ncx_template, # You might need to manually define this if not easily importable
)
from ...models import Chapter, Novel, Volume
from ...utils.imgen import build_cover
from ..app import App

# Define templates here if they are not importable directly
# These are simplified versions; adjust based on the actual content needed.
# Ensure lncrawl/assets/epub/style.css exists in your project.
try:
    with open(os.path.join(os.path.dirname(__file__), '../../assets/epub/style.css'), 'r') as f:
        style_css_template = f.read()
except FileNotFoundError:
    style_css_template = "/* Default styles */ body { font-family: sans-serif; }"
    print("Warning: Could not find style.css template.")

chapter_xhtml_template = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>{title}</title>
    <link href="style.css" rel="stylesheet" type="text/css"/>
</head>
<body>
    <h3>{title}</h3>
    {body}
</body>
</html>
"""

cover_xhtml_template = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>Cover</title>
    <link href="style.css" rel="stylesheet" type="text/css" />
</head>
<body>
    <div id="cover-image">
        <img src="cover.jpg" alt="{title}" />
    </div>
</body>
</html>
"""

# Placeholder for content.opf, nav.xhtml, toc.ncx - ebooklib usually handles these.

logger = logging.getLogger('EPUB_BINDER')


class EpubBinder:
    executor: concurrent.futures.ThreadPoolExecutor

    def __init__(self, app: App):
        self.app = app
        self.epub = epub.EpubBook()
        self.chapters: List[Chapter] = []
        self.volumes: List[Volume] = []
        self.available_formats = ["epub"]

    def create_book(self) -> epub.EpubBook:
        """Create the EPUB book structure"""
        self.epub = epub.EpubBook()
        self.epub.set_identifier(self.app.crawler.novel_url)
        self.epub.set_title(self.app.crawler.novel_title or 'Unknown')
        self.epub.set_language(self.app.crawler.locale or 'en')
        if self.app.crawler.novel_author:
            author_list = [
                x.strip()
                for x in re.split(r",|;|\|", self.app.crawler.novel_author)
                if x.strip()
            ]
            for author in author_list:
                self.epub.add_author(author)
        else:
             self.epub.add_author('Unknown')

        # Add metadata
        self.epub.add_metadata('DC', 'description', self.app.crawler.novel_summary or 'No summary available.')
        self.epub.add_metadata(None, 'meta', '', {'property': 'dcterms:modified', 'content': datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')})
        self.epub.add_metadata(None, 'meta', 'lncrawl', {'name': 'generator'})

        # Add default cover
        logger.info('Adding cover: %s', self.app.book_cover)
        if self.app.book_cover:
            try:
                self.epub.set_cover('cover.jpg', open(self.app.book_cover, 'rb').read())
            except Exception as e:
                logger.warn(f"Failed to set cover image: {e}")
                self.add_missing_cover()
        else:
             self.add_missing_cover()

        # Add CSS file
        nav_css = epub.EpubItem(uid="style_nav", file_name="style.css", media_type="text/css", content=style_css_template)
        self.epub.add_item(nav_css)

        self.epub.spine = ['nav'] # Add nav first
        self.epub.guide = [{'type': 'cover', 'title': 'Cover', 'href': 'cover.xhtml'}] # Add cover guide if cover exists
        self.epub.toc = [] # Initialize TOC list

        return self.epub

    def add_missing_cover(self):
        try:
            logger.info("Generating default cover...")
            img = build_cover(
                self.app.crawler.novel_title,
                self.app.crawler.novel_author
            )
            if img:
                 cover_path = os.path.join(self.app.output_path, 'cover.png')
                 img.save(cover_path, format='PNG')
                 self.epub.set_cover('cover.png', open(cover_path, 'rb').read())
                 logger.info("Added generated cover.")
            else:
                 logger.warn("Failed to generate default cover.")
        except Exception as e:
            logger.exception("Failed generating default cover image: %s", e)


    def bind_epub_book(self):
        """Processes chapters and binds them into the EPUB"""
        epub_book = self.create_book()
        toc_items = {} # Store items for NCX/Nav generation: {vol_id: [chapters]}

        logger.info('Binding %d chapters...', len(self.chapters))
        pbar = tqdm(total=len(self.chapters), desc='EPUB Binding', unit='Ch')
        for chapter in self.chapters:
            # Check if crawling was stopped
            if not self.app.crawler.app.running:
                logger.warn("Detected stop signal. Aborting EPUB binding.")
                return None # Indicate failure/stop

            try:
                # 1. Get Chapter Content (reuse existing download or fetch if needed)
                # Assuming chapter content might be pre-downloaded or needs fetching here.
                # If content is in chapter['body'], use it. Otherwise, fetch it.
                # This part depends heavily on how lncrawl handles chapter data.
                # Simplified: fetch content directly. Adapt if pre-downloaded.
                # Ensure download_chapter_body handles potential errors.
                body = self.app.crawler.download_chapter_body(chapter)
                if not body:
                    logger.warn(f"Skipping Chapter {chapter['id']} due to empty body.")
                    pbar.update(1)
                    continue

                # 2. Process Content (Basic cleaning - enhance as needed)
                content = self.app.crawler.cleanup_text(body)

                # 3. Create EPUB chapter item
                epub_chapter = epub.EpubHtml(
                    title=chapter['title'] or f"Chapter {chapter['id']}",
                    file_name=f"chapter_{chapter['id']:05}.xhtml",
                    lang=self.app.crawler.locale or 'en'
                )
                epub_chapter.content = chapter_xhtml_template.format(
                    title=chapter['title'] or f"Chapter {chapter['id']}",
                    body=content
                )
                epub_book.add_item(epub_chapter)
                epub_book.spine.append(epub_chapter) # Add chapter to reading order

                # 4. Add to TOC structure
                vol_id = chapter['volume']
                if vol_id not in toc_items:
                    toc_items[vol_id] = []
                toc_items[vol_id].append(epub_chapter)

                # 5. Clear content from memory (important for low-resource environments)
                chapter['body'] = None # Clear if it was stored
                body = None
                content = None
                epub_chapter.content = None # Let ebooklib handle content writing later

            except Exception as e:
                logger.error(f"Failed to process chapter {chapter['id']}: {e}", exc_info=True)
            finally:
                 pbar.update(1) # Ensure progress bar updates even on error

        pbar.close()

        # Generate TOC and Nav after all chapters are processed
        try:
            epub_book.toc = []
            volume_map = {vol['id']: vol['title'] for vol in self.volumes}

            for vol_id in sorted(toc_items.keys()):
                vol_title = volume_map.get(vol_id) or f"Volume {vol_id}"
                # Create a section for the volume
                section = (epub.Section(vol_title), tuple(toc_items[vol_id]))
                epub_book.toc.append(section)

            # Add navigation files
            epub_book.add_item(epub.EpubNcx())
            epub_book.add_item(epub.EpubNav())

        except Exception as e:
            logger.error(f"Failed to generate TOC/Nav: {e}", exc_info=True)

        return epub_book

    def get_output_path(self) -> str:
        """Determines the final EPUB file path"""
        epub_name = f"{self.app.good_file_name}.epub"
        return os.path.join(self.app.output_path, epub_name)

    def write_epub(self, epub_book: epub.EpubBook):
        """Writes the EPUB file to disk"""
        output_path = self.get_output_path()
        logger.info(f"Writing EPUB to: {output_path}")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        try:
            epub.write_epub(output_path, epub_book, {})
            logger.info("EPUB binding finished.")
        except Exception as e:
            logger.error(f"Failed to write EPUB file: {e}", exc_info=True)
            raise # Re-raise exception to indicate failure

    def process_and_write_epub(self):
        """High-level function to orchestrate EPUB creation and writing"""
        self.chapters = self.app.crawler.chapters # Get chapters from crawler
        self.volumes = self.app.crawler.volumes   # Get volumes from crawler

        if not self.chapters:
             logger.warn("No chapters found to bind.")
             return False

        epub_book = self.bind_epub_book()
        if epub_book: # Check if binding was successful (not aborted)
            self.write_epub(epub_book)
            return True
        else:
            logger.warn("EPUB binding was aborted or failed.")
            return False

    # Note: `self.dump_output` and `self.bind` methods from the original
    # file might need to be adapted or called differently depending on
    # how App class uses this binder. This simplified version focuses
    # on the memory issue in EPUB creation itself.
    # The `send_novel` function in telegram_bot.py seems to call `app.pack_epub()`.
    # Ensure `app.pack_epub()` eventually calls `process_and_write_epub()`.
