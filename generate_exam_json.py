
#!/usr/bin/env python3
"""
generate_exam_json.py
----------------------------------------------------
High-reliability scraper for ExamTopics discussion pages.
Features:
- Browser TLS fingerprint impersonation (curl_cffi) to bypass Cloudflare bot heuristics
- Two-tier caching/checkpointing (listing links + individual question JSONs)
- Automatic global backoff on HTTP 429 rate-limiting
- Resumable anytime without repeating requests
"""

import os
import re
import sys
import json
import time
import random
import logging
import argparse
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from bs4 import BeautifulSoup

# Try importing curl_cffi for realistic Chrome TLS fingerprinting
try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests
    from requests.adapters import HTTPAdapter, Retry
    USE_CURL_CFFI = False

# ---------- CONFIG ----------
DEFAULT_DELAY = 5.0
TIMEOUT = 30
MAX_RETRIES = 5
DEFAULT_CONCURRENCY = 1
RATE_LIMIT_WAIT = 15.0
DEFAULT_DOH_URL = "https://1.1.1.1/dns-query"
# ----------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Global thread-safe rate limit backoff coordination
_rate_limit_lock = Lock()
_cooldown_until = 0.0

def wait_for_rate_limit(delay_seconds: float):
    """Enforce shared delay and rate limit cooldown across all worker threads."""
    global _cooldown_until
    with _rate_limit_lock:
        now = time.time()
        if _cooldown_until > now:
            pause = _cooldown_until - now
            logging.warning("Rate limit active. Pausing thread for %.1f seconds...", pause)
            time.sleep(pause)
        time.sleep(delay_seconds + random.uniform(0.1, 0.5))

def trigger_cooldown(wait_seconds: float):
    """Set global backoff window when HTTP 429 is received."""
    global _cooldown_until
    with _rate_limit_lock:
        _cooldown_until = max(_cooldown_until, time.time() + wait_seconds)
        logging.warning("HTTP 429 detected! Backing off all requests for %.0f seconds...", wait_seconds)

def parse_cookie_string(cookie_str: str) -> dict:
    """Parse cookie string into a key-value dictionary."""
    cookies = {}
    if not cookie_str:
        return cookies
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies

def make_session(cookies: dict | None = None, doh_url: str | None = DEFAULT_DOH_URL) -> requests.Session:
    """Create HTTP session using curl_cffi with Chrome impersonation or standard requests."""
    if USE_CURL_CFFI:
        kwargs = {"impersonate": "chrome120"}
        if doh_url:
            kwargs["doh_url"] = doh_url
        s = requests.Session(**kwargs)
    else:
        s = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retries))
        s.mount("http://", HTTPAdapter(max_retries=retries))

    s.headers.update(DEFAULT_HEADERS)
    if cookies:
        if USE_CURL_CFFI:
            for k, v in cookies.items():
                s.cookies.set(k, v)
        else:
            s.cookies.update(cookies)
    return s

def fetch_url(url: str, session: requests.Session, delay_seconds: float, max_attempts: int = MAX_RETRIES) -> str | None:
    """Fetches a URL with global rate-limit cooldown and automatic 429 retry."""
    for attempt in range(1, max_attempts + 1):
        wait_for_rate_limit(delay_seconds)
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                logging.info("URL %s returned 404", url)
                return None
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = float(retry_after)
                else:
                    wait_time = RATE_LIMIT_WAIT * attempt
                logging.warning("429 Too Many Requests for %s (attempt %d/%d)", url, attempt, max_attempts)
                trigger_cooldown(wait_time)
                continue
            r.raise_for_status()
        except Exception as e:
            logging.warning("Attempt %d/%d error for %s: %s", attempt, max_attempts, url, e)
            if attempt < max_attempts:
                time.sleep(2 * attempt)
            else:
                logging.error("Failed to fetch %s after %d attempts", url, max_attempts)
                return None
    return None

def extract_links(html: str, base_url: str):
    """Extract discussion links from a listing page (supporting both absolute and relative URLs)."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        parsed = urlparse(href)
        path = parsed.path if parsed.path else href
        if not path.startswith("/discussions/"):
            continue
        if "/view/" not in path:
            continue
        title = a.get_text(strip=True)
        if title:
            links.append((title, urljoin(base_url, href)))
    return links

def extract_question_data(soup: BeautifulSoup, url: str) -> dict:
    """Extracts structured data for a single question."""
    data = {"url": url}

    # Extract title
    title_tag = soup.find("h1", class_="discussion-list-header")
    if not title_tag:
        title_tag = soup.find("title")
    data["title"] = title_tag.get_text(strip=True) if title_tag else "No Title"

    # Extract topic and question number from title
    topic_match = re.search(r"topic\W*(\d+)", data["title"], re.I)
    question_match = re.search(r"question\W*(\d+)", data["title"], re.I)
    data["topic"] = f"Topic {int(topic_match.group(1))}" if topic_match else "Misc"
    data["question_number"] = int(question_match.group(1)) if question_match else None

    # Extract question text and choices
    question_body = soup.find("div", class_="question-body")
    raw_text = ""
    if question_body:
        p_tag = question_body.find("p", class_="card-text")
        if p_tag:
            raw_text = p_tag.get_text(strip=True, separator="\n")

    choices = []
    choices_container = soup.find("div", class_="question-choices-container")
    if choices_container:
        for choice_item in choices_container.find_all("li", class_="multi-choice-item"):
            letter_tag = choice_item.find("span", class_="multi-choice-letter")
            if letter_tag:
                letter = letter_tag.get_text(strip=True).replace(".", "")
                text = choice_item.get_text(strip=True).replace(letter + ".", "").strip()
                choices.append({"letter": letter, "text": text})

    # Fallback: if choices are rendered directly inside p.card-text
    if not choices and raw_text:
        parts = re.split(r"\n(?=[A-Z]\.\s*)", raw_text)
        if len(parts) > 1:
            data["question_text"] = parts[0].strip()
            for p in parts[1:]:
                m = re.match(r"^([A-Z])\.\s*(.*)", p.strip(), re.DOTALL)
                if m:
                    choices.append({"letter": m.group(1), "text": m.group(2).strip()})
        else:
            data["question_text"] = raw_text
    else:
        data["question_text"] = raw_text

    data["choices"] = choices

    # Extract correct answer
    correct_answer_span = soup.find("span", class_="correct-answer")
    if correct_answer_span:
        data["correct_answer"] = correct_answer_span.get_text(strip=True)
    else:
        data["correct_answer"] = None

    # Extract vote distribution from voting-summary div
    data["vote_distribution"] = []
    voting_summary = soup.find("div", class_="voting-summary")
    if voting_summary:
        vote_bars = voting_summary.find_all("div", class_="vote-bar")
        for bar in vote_bars:
            text = bar.get_text(strip=True)
            match = re.search(r"([A-Z]) \((\d+)%\)", text)
            if match:
                answer_letter = match.group(1)
                percentage = int(match.group(2))
                data["vote_distribution"].append({"answer": answer_letter, "percentage": percentage})

    # Extract discussion
    discussion = []
    for comment in soup.find_all("div", class_="comment-container"):
        author_tag = comment.find("h5", class_="comment-username")
        date_tag = comment.find("span", class_="comment-date")
        comment_content_tag = comment.find("div", class_="comment-content")
        ans_tag = comment.find("div", class_=lambda cl: cl and ("badge-warning" in cl or "comment-selected-answers" in cl))

        if author_tag and comment_content_tag:
            selected_answer = None
            if ans_tag:
                span = ans_tag.find("span")
                selected_answer = span.get_text(strip=True) if span else ans_tag.get_text(strip=True).replace("Selected Answer:", "").strip()
            discussion.append({
                "author": author_tag.get_text(strip=True),
                "date": date_tag.get("title") if date_tag else None,
                "comment": comment_content_tag.get_text(strip=True, separator="\n"),
                "selected_answer": selected_answer,
            })
    data["discussion"] = discussion

    return data

def get_question_cache_key(url: str) -> str:
    """Generate a unique filesystem-safe key for question caching."""
    m = re.search(r"/view/(\d+)", url)
    if m:
        return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:16]

def save_links_checkpoint(links_file: Path, keyword: str, last_crawled_page: int, links: list):
    """Save discovered links to disk checkpoint."""
    payload = {
        "keyword": keyword,
        "last_crawled_page": last_crawled_page,
        "links": links,
    }
    with open(links_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def load_links_checkpoint(links_file: Path) -> tuple[int, list]:
    """Load previously discovered links from disk checkpoint."""
    if not links_file.exists():
        return 0, []
    try:
        with open(links_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("last_crawled_page", 0), data.get("links", [])
        if isinstance(data, list):
            return 0, data
    except Exception as e:
        logging.warning("Could not read links checkpoint (%s): %s", links_file, e)
    return 0, []

def crawl_pages_parallel(
    base_url: str,
    keyword: str,
    start_page: int,
    num_pages: int,
    crawl_concurrency: int,
    delay_seconds: float,
    links_file: Path,
    cookies: dict | None = None,
    doh_url: str | None = DEFAULT_DOH_URL,
):
    """Crawl listing pages in order and collect matching links with automatic disk checkpointing."""
    last_page, existing_links = load_links_checkpoint(links_file)
    collected = list(existing_links)
    seen = {item["url"] for item in collected}

    if collected:
        logging.info("Loaded %d existing links from checkpoint %s (last page crawled: %d)", len(collected), links_file.name, last_page)
        # Automatically resume from the next page if user didn't explicitly override start_page
        if start_page == 1 and last_page > 0:
            start_page = last_page + 1
            logging.info("Auto-resuming crawl from page %d", start_page)

    base_url = base_url.rstrip("/") + "/"
    target_end_page = start_page + num_pages - 1
    page_urls = [(i, urljoin(base_url, f"{i}/")) for i in range(start_page, target_end_page + 1)]
    lock = Lock()
    completed_count = 0
    consecutive_empty = 0
    stop_crawling = False
    total_pages = len(page_urls)

    def fetch_listing(page_num, url, session):
        nonlocal completed_count, consecutive_empty, stop_crawling
        if stop_crawling:
            return []

        html = fetch_url(url, session, delay_seconds)
        with lock:
            completed_count += 1
            if completed_count % 10 == 0 or completed_count == total_pages:
                logging.info("Progress: crawled %d/%d listing pages", completed_count, total_pages)

        if not html:
            return []

        links = extract_links(html, url)
        with lock:
            if not links:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    logging.info("Reached end of discussion pages at page %d. Stopping listing crawl.", page_num)
                    stop_crawling = True
            else:
                consecutive_empty = 0

        matches = []
        for title, link in links:
            if keyword.lower() in title.lower():
                matches.append({"title": title, "url": link})

        with lock:
            added = 0
            for it in matches:
                if it["url"] not in seen:
                    seen.add(it["url"])
                    collected.append(it)
                    added += 1
            if added:
                logging.info("Found %d new matches on %s (Total so far: %d)", added, url, len(collected))
            save_links_checkpoint(links_file, keyword, page_num, collected)
        return matches

    logging.info(
        "Crawling %d listing pages (pages %d..%d) with concurrency=%d, delay=%.1fs",
        total_pages, start_page, target_end_page, crawl_concurrency, delay_seconds
    )

    def worker_task(item):
        page_num, url = item
        if not hasattr(worker_task, "_session"):
            worker_task._session = make_session(cookies, doh_url=doh_url)
        return fetch_listing(page_num, url, worker_task._session)

    with ThreadPoolExecutor(max_workers=crawl_concurrency) as ex:
        futures = {ex.submit(worker_task, item): item for item in page_urls}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logging.debug("Listing worker error: %s", e)

    save_links_checkpoint(links_file, keyword, start_page + completed_count - 1, collected)
    logging.info("Crawl complete. Total matches saved to %s: %d", links_file.name, len(collected))
    return collected

def fetch_and_process_page(item: dict, session: requests.Session, delay_seconds: float, cache_dir: Path):
    """Fetches a single discussion page with file-based caching."""
    url = item["url"]
    qid = get_question_cache_key(url)
    cache_path = cache_dir / f"{qid}.json"

    # 1. Return from disk cache if already downloaded
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass  # Re-download if file is corrupted

    # 2. Fetch from web
    logging.info("Downloading %s", url)
    html = fetch_url(url, session, delay_seconds)
    if not html:
        logging.error("Failed to fetch %s", url)
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
        data = extract_question_data(soup, url)
        # 3. Immediately save question to disk cache
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return data
    except Exception as e:
        logging.error("Failed to parse %s: %s", url, e)
        return None

def main():
    parser = argparse.ArgumentParser(description="Download ExamTopics discussions to a JSON file with rate-limit evasion & checkpointing.")
    parser.add_argument("--base", required=True, help="Base discussion URL (e.g. https://www.examtopics.com/discussions/microsoft/)")
    parser.add_argument("--keyword", required=True, help="Keyword or exam name (e.g. AZ-104)")
    parser.add_argument("--pages", type=int, default=10, help="Number of listing pages to crawl")
    parser.add_argument("--start-page", type=int, default=1, help="Page number to start crawling from (default: 1)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"Number of parallel downloads (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Per-request delay in seconds (default: {DEFAULT_DELAY})")
    parser.add_argument("--output", type=str, default="questions.json", help="Output JSON file name")
    parser.add_argument("--links-file", type=str, default=None, help="File to save/load discovered discussion links (default: <output>_links.json)")
    parser.add_argument("--cache-dir", type=str, default=None, help="Directory for caching downloaded question files")
    parser.add_argument("--skip-crawl", action="store_true", help="Skip crawling listing pages and load URLs directly from links file")
    parser.add_argument("--cookie", type=str, default=None, help="Browser cookie string (e.g. 'cf_clearance=...; PHPSESSID=...')")
    parser.add_argument("--doh-url", type=str, default=DEFAULT_DOH_URL, help=f"DNS-over-HTTPS resolver URL (default: {DEFAULT_DOH_URL}) to bypass local DNS sinkholes/blocks")
    parser.add_argument("--no-resequence", action="store_true", help="Do not re-sequence question numbers 1..N across topics (default is to sequence them)")
    args = parser.parse_args()

    engine_name = "curl_cffi (Chrome TLS impersonation)" if USE_CURL_CFFI else "standard requests"
    logging.info("HTTP Engine: %s", engine_name)

    doh_url = None if args.doh_url.lower() in ("none", "off", "false", "") else args.doh_url
    if doh_url:
        logging.info("DoH Resolver: %s (bypassing local DNS inspection)", doh_url)

    output_path = Path(args.output).resolve()
    stem = output_path.stem

    # Set up checkpoints
    links_file = Path(args.links_file).resolve() if args.links_file else output_path.parent / f"{stem}_links.json"
    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else output_path.parent / f".cache_{stem}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cookies = parse_cookie_string(args.cookie)

    # Phase 1: Listing Links
    if args.skip_crawl:
        _, results = load_links_checkpoint(links_file)
        if not results:
            logging.error("No links found in %s! Run without --skip-crawl first.", links_file)
            return
        logging.info("Loaded %d links directly from %s (skipping listing crawl)", len(results), links_file.name)
    else:
        results = crawl_pages_parallel(
            base_url=args.base,
            keyword=args.keyword,
            start_page=args.start_page,
            num_pages=args.pages,
            crawl_concurrency=max(1, args.concurrency),
            delay_seconds=args.delay,
            links_file=links_file,
            cookies=cookies,
            doh_url=doh_url,
        )

    if not results:
        print("No matches found.")
        return

    # Check how many are already cached
    cached_count = sum(1 for item in results if (cache_dir / f"{get_question_cache_key(item['url'])}.json").exists())
    print(f"\nProcessing {len(results)} discussion pages ({cached_count} already cached in {cache_dir.name})...")

    # Phase 2: Question Downloads
    all_questions = []

    def detail_worker(item):
        if not hasattr(detail_worker, "_session"):
            detail_worker._session = make_session(cookies, doh_url=doh_url)
        return fetch_and_process_page(item, detail_worker._session, args.delay, cache_dir)

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(detail_worker, item): item for item in results}
        for fut in as_completed(futures):
            try:
                data = fut.result()
                if data:
                    all_questions.append(data)
            except Exception as e:
                logging.error("Worker error: %s", e)

    # Naturally sort questions by topic number and question number
    def sort_key(item):
        topic_str = item.get("topic", "")
        m_top = re.search(r"(\d+)", topic_str)
        topic_num = int(m_top.group(1)) if m_top else 999999
        q_num = item.get("question_number") or 0
        return (topic_num, q_num)

    all_questions.sort(key=sort_key)

    # Re-sequence question numbers sequentially across the exam
    if not args.no_resequence:
        for idx, q in enumerate(all_questions, start=1):
            q["question_number"] = idx

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_questions, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Finished! {len(all_questions)} questions saved to: {output_path}")
    print(f"📁 Individual questions cached in: {cache_dir}")
    print(f"🔗 Links checkpoint saved in: {links_file}")

if __name__ == "__main__":
    main()
