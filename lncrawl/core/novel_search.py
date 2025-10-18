import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Dict, List
from urllib.parse import urlparse

from slugify import slugify

from ..models import CombinedSearchResult, SearchResult
from .sources import prepare_crawler

logger = logging.getLogger(__name__)

# This function will be executed in a thread for each site.
def _search_a_site(link: str, query: str) -> List[SearchResult]:
    """Searches a single site for a novel."""
    try:
        # Each thread gets its own crawler instance.
        crawler = prepare_crawler(link)
        setattr(crawler, 'can_use_browser', False)  # Disable browser in search
        
        results = []
        # The original crawler's search_novel method is a generator.
        for item in crawler.search_novel(query):
            if not isinstance(item, SearchResult):
                item = SearchResult(**item)
            if item.url and item.title:
                item.title = item.title.lower().title()
                results.append(item)
        return results
    except Exception:
        # Log exceptions but don't crash the entire search.
        if logger.isEnabledFor(logging.DEBUG):
            logger.exception(f'<< {link} >> Search failed')
        return []

def search_novels(app):
    """
    Searches for novels using a thread pool for concurrency.
    This is much more efficient than multiprocessing in a constrained environment.
    """
    from .app import App
    from .sources import crawler_list

    assert isinstance(app, App)

    if not app.crawler_links or not app.user_input:
        return

    # Use a ThreadPoolExecutor for I/O-bound tasks.
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        checked_crawlers = set()

        for link in app.crawler_links:
            hostname = urlparse(link).hostname
            CrawlerType = crawler_list.get(hostname or '')
            if not CrawlerType or CrawlerType in checked_crawlers:
                continue
            
            checked_crawlers.add(CrawlerType)
            # Submit the search task to the thread pool.
            future = executor.submit(_search_a_site, link, app.user_input)
            futures[future] = hostname
        
        all_results = []
        progress = 0
        total_futures = len(futures)
        app.search_progress = 0

        # Process results as they are completed.
        for future in as_completed(futures):
            try:
                # The result() call will re-raise exceptions if the task failed.
                all_results.extend(future.result() or [])
            except Exception as e:
                hostname = futures.get(future, "unknown site")
                logger.error(f"Search task for {hostname} failed: {e}")
            
            progress += 1
            app.search_progress = 100 * progress / total_futures

    # --- Result Combination Logic (from your original file) ---
    combined: Dict[str, List[SearchResult]] = {}
    for item in all_results:
        if not (item and item.title):
            continue
        key = slugify(str(item.title))
        if len(key) <= 2:
            continue
        combined.setdefault(key, [])
        combined[key].append(item)

    processed: List[CombinedSearchResult] = []
    for key, value in combined.items():
        value.sort(key=lambda x: x.url)
        processed.append(
            CombinedSearchResult(
                id=key,
                title=value[0].title,
                novels=value,
            )
        )
    processed.sort(
        key=lambda x: (
            -len(x.novels),
            -SequenceMatcher(a=x.title, b=app.user_input).ratio(),
        )
    )
    app.search_results = processed[:10] # Limit to top 10 results
