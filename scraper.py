#!/usr/bin/env python3
"""Scrape public yuaigongwu.com forum threads whose titles match a keyword."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


DEFAULT_FORUM_URL = "http://www.yuaigongwu.com/forum-145-279.html"
USER_AGENT = "YuaigongwuResearchScraper/1.0 (polite public-page research scraper)"
THREAD_ID_RE = re.compile(r"(?:thread-|[?&]tid=)(\d+)")
POST_ID_RE = re.compile(r"post_(\d+)$")
DATE_RE = re.compile(r"发表于\s*([^|]+)")
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class UnexpectedPageError(RuntimeError):
    """The response is not the requested public forum page."""


class RetryableResponseError(requests.RequestException):
    """The server returned a response that should be retried."""


@dataclass(frozen=True)
class ThreadLink:
    thread_id: str
    title: str
    url: str
    source_forum_url: str


def clean_text(node: Tag | None) -> str:
    """Return readable post text without site UI/login notices."""
    if node is None:
        return ""
    copy = BeautifulSoup(str(node), "html.parser")
    for unwanted in copy.select(
        "script, style, .attach_nopermission, .pstatus, .jammer, .showhide"
    ):
        unwanted.decompose()
    text = copy.get_text("\n", strip=True)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def absolute_asset_urls(node: Tag, page_url: str, selector: str, attr: str) -> list[str]:
    values: list[str] = []
    for element in node.select(selector):
        value = element.get(attr)
        if value and not value.startswith(("javascript:", "data:")):
            absolute = urljoin(page_url, value)
            if absolute not in values:
                values.append(absolute)
    return values


class ForumScraper:
    def __init__(
        self,
        delay: float = 1.5,
        timeout: float = 30.0,
        retries: int = 5,
        backoff: float = 3.0,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
        )
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.delay / 4)))

    @staticmethod
    def _validate_destination(requested_url: str, final_url: str) -> None:
        requested_host = (urlparse(requested_url).hostname or "").removeprefix("www.")
        final_host = (urlparse(final_url).hostname or "").removeprefix("www.")
        if requested_host != final_host:
            raise UnexpectedPageError(
                f"Request was redirected away from {requested_host} to {final_host}. "
                "This usually means the network returned a filtering/block page."
            )

    def get_soup(self, url: str) -> BeautifulSoup:
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            logging.info("GET %s", url)
            try:
                response = self.session.get(url, timeout=self.timeout)
                self._validate_destination(url, response.url)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise RetryableResponseError(
                        f"server returned HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                # The forum declares UTF-8. Statistical detection misidentifies
                # these mostly-Chinese pages and produces mojibake.
                response.encoding = response.encoding or "utf-8"
                return BeautifulSoup(response.text, "html.parser")
            except requests.exceptions.SSLError as exc:
                raise RuntimeError(
                    "The site's HTTPS certificate could not be verified. "
                    "Use its public HTTP URL (http://www.yuaigongwu.com/...) instead."
                ) from exc
            except UnexpectedPageError:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    RetryableResponseError) as exc:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"Request failed after {attempts} attempt(s): {url}: {exc}"
                    ) from exc
                pause = self.backoff * (2 ** (attempt - 1))
                pause += random.uniform(0, min(1.0, max(0.1, pause / 5)))
                logging.warning(
                    "Temporary request failure (%s/%s): %s; retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    pause,
                )
                time.sleep(pause)
            finally:
                self._last_request_at = time.monotonic()
        raise AssertionError("unreachable")

    @staticmethod
    def validate_listing_page(soup: BeautifulSoup, page_url: str) -> None:
        if soup.select_one("#threadlisttableid") is None:
            title = soup.title.get_text(" ", strip=True) if soup.title else "untitled page"
            raise UnexpectedPageError(
                f"Expected a forum listing at {page_url}, but received {title!r}."
            )

    @staticmethod
    def validate_thread_page(soup: BeautifulSoup, page_url: str) -> None:
        if soup.select_one("#thread_subject") is None or soup.select_one("#postlist") is None:
            title = soup.title.get_text(" ", strip=True) if soup.title else "untitled page"
            raise UnexpectedPageError(
                f"Expected a thread at {page_url}, but received {title!r}."
            )

    def listing_threads(self, soup: BeautifulSoup, page_url: str) -> list[ThreadLink]:
        """Extract only real thread rows, excluding recommendations/sidebars."""
        found: list[ThreadLink] = []
        seen: set[str] = set()
        for row in soup.select("#threadlisttableid tbody[id^='normalthread_']"):
            anchor = row.select_one("a[onclick*='atarget'][href]")
            if anchor is None:
                continue
            match = THREAD_ID_RE.search(anchor.get("href", ""))
            if not match:
                continue
            thread_id = match.group(1)
            if thread_id in seen:
                continue
            seen.add(thread_id)
            title = anchor.get_text(" ", strip=True)
            # Use a stable first-page URL, independent of listing-page extras.
            url = urljoin(page_url, f"thread-{thread_id}-1-1.html")
            found.append(ThreadLink(thread_id, title, url, page_url))
        return found

    @staticmethod
    def next_listing_url(soup: BeautifulSoup, page_url: str) -> str | None:
        anchor = soup.select_one("#fd_page_bottom a.nxt[href]")
        return urljoin(page_url, anchor["href"]) if anchor else None

    @staticmethod
    def next_thread_url(
        soup: BeautifulSoup, page_url: str, thread_id: str
    ) -> str | None:
        for anchor in soup.select("div.pg a.nxt[href]"):
            href = anchor.get("href", "")
            match = THREAD_ID_RE.search(href)
            if match and match.group(1) == thread_id:
                return urljoin(page_url, href)
            if f"thread-{thread_id}-" in href:
                return urljoin(page_url, href)
        return None

    @staticmethod
    def _opening_metadata(soup: BeautifulSoup) -> tuple[str, str]:
        header = soup.select_one(".comiis_v_action span.xg1")
        if not header:
            return "", ""
        author_node = header.select_one("a[href*='space-uid-']")
        author = author_node.get_text(" ", strip=True) if author_node else ""
        match = DATE_RE.search(header.get_text(" ", strip=True))
        return author, match.group(1).strip() if match else ""

    def parse_posts(
        self, soup: BeautifulSoup, page_url: str, page_number: int
    ) -> list[dict]:
        opening_author, opening_date = self._opening_metadata(soup)
        posts: list[dict] = []
        for wrapper in soup.select("#postlist div[id^='post_']"):
            wrapper_id = wrapper.get("id", "")
            match = POST_ID_RE.fullmatch(wrapper_id)
            if not match:
                continue
            post_id = match.group(1)
            body = wrapper.select_one(f"#postmessage_{post_id}")
            if body is None:
                continue

            floor_node = wrapper.select_one(f"#postnum{post_id}")
            floor = floor_node.get_text(" ", strip=True) if floor_node else ""
            is_opening = floor == "楼主"
            auth = wrapper.select_one(".authi")
            author_node = auth.select_one("a.kmxi2, a[c='1']") if auth else None
            date_node = wrapper.select_one("em[id^='authorposton']")
            date_match = DATE_RE.search(date_node.get_text(" ", strip=True)) if date_node else None

            posts.append(
                {
                    "post_id": post_id,
                    "is_opening_post": is_opening,
                    "floor": floor,
                    "author": opening_author
                    if is_opening
                    else (author_node.get_text(" ", strip=True) if author_node else ""),
                    "posted_at": opening_date
                    if is_opening
                    else (date_match.group(1).strip() if date_match else ""),
                    "content": clean_text(body),
                    "image_urls": absolute_asset_urls(body, page_url, "img[src]", "src"),
                    "link_urls": absolute_asset_urls(body, page_url, "a[href]", "href"),
                    "thread_page": page_number,
                    "source_url": page_url,
                }
            )
        return posts

    def scrape_thread(self, link: ThreadLink) -> dict:
        page_url: str | None = link.url
        visited_pages: set[str] = set()
        seen_posts: set[str] = set()
        all_posts: list[dict] = []
        page_number = 1
        parsed_title = link.title

        while page_url and page_url not in visited_pages:
            visited_pages.add(page_url)
            soup = self.get_soup(page_url)
            self.validate_thread_page(soup, page_url)
            title_node = soup.select_one("#thread_subject")
            if title_node:
                parsed_title = title_node.get_text(" ", strip=True)

            for post in self.parse_posts(soup, page_url, page_number):
                if post["post_id"] not in seen_posts:
                    seen_posts.add(post["post_id"])
                    all_posts.append(post)
            page_url = self.next_thread_url(soup, page_url, link.thread_id)
            page_number += 1

        opening = next((p for p in all_posts if p["is_opening_post"]), None)
        if opening is None and all_posts:
            opening = all_posts[0]
            opening["is_opening_post"] = True
        comments = [p for p in all_posts if p is not opening]
        return {
            "thread_id": link.thread_id,
            "title": parsed_title,
            "url": link.url,
            "source_forum_url": link.source_forum_url,
            "theme": opening["content"] if opening else "",
            "opening_post": opening,
            "comments": comments,
            "comment_count_scraped": len(comments),
            "thread_pages_scraped": len(visited_pages),
        }

CSV_FIELDS = [
    "thread_id",
    "thread_title",
    "thread_url",
    "post_id",
    "is_opening_post",
    "floor",
    "author",
    "posted_at",
    "content",
    "image_urls",
    "link_urls",
    "thread_page",
    "source_url",
]


def thread_csv_rows(thread: dict):
    posts = ([thread["opening_post"]] if thread["opening_post"] else []) + thread["comments"]
    for post in posts:
        yield {
            "thread_id": thread["thread_id"],
            "thread_title": thread["title"],
            "thread_url": thread["url"],
            **post,
            "image_urls": json.dumps(post["image_urls"], ensure_ascii=False),
            "link_urls": json.dumps(post["link_urls"], ensure_ascii=False),
        }


def write_csv(path: Path, threads: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for thread in threads:
            writer.writerows(thread_csv_rows(thread))
        handle.flush()
        os.fsync(handle.fileno())


def append_thread_jsonl(path: Path, thread: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(thread, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_thread_csv(path: Path, thread: dict) -> None:
    file_exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(thread_csv_rows(thread))
        handle.flush()
        os.fsync(handle.fileno())


def load_jsonl(path: Path) -> list[dict]:
    """Load durable results and repair only a truncated final line."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    threads: list[dict] = []
    seen: set[str] = set()
    repair_needed = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            thread = json.loads(line)
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise RuntimeError(f"Invalid JSONL data at {path}:{index + 1}")
            logging.warning("Removing a truncated final JSONL line from %s", path)
            repair_needed = True
            break
        thread_id = str(thread.get("thread_id", ""))
        if not thread_id or thread_id in seen:
            repair_needed = True
            continue
        seen.add(thread_id)
        threads.append(thread)
    if repair_needed:
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for thread in threads:
                handle.write(json.dumps(thread, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    return threads


def save_checkpoint(path: Path, state: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load_checkpoint(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forum-url", default=DEFAULT_FORUM_URL)
    parser.add_argument("--keyword", default="肝癌")
    parser.add_argument(
        "--forum-pages",
        type=int,
        default=1,
        help="listing pages to scan; use 0 to follow all remaining pages (default: 1)",
    )
    parser.add_argument("--max-threads", type=int, default=0, help="0 means no limit")
    parser.add_argument("--delay", type=float, default=1.5, help="minimum seconds between requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=5, help="retries after timeouts/5xx/429")
    parser.add_argument("--backoff", type=float, default=3.0, help="initial retry backoff seconds")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="discard this output directory's checkpoint/results and start over",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if min(args.forum_pages, args.max_threads, args.retries) < 0:
        raise SystemExit("--forum-pages, --max-threads, and --retries must be zero or positive")

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "threads.jsonl"
    csv_path = output_dir / "posts.csv"
    checkpoint_path = output_dir / "checkpoint.json"

    if args.fresh:
        for path in (jsonl_path, csv_path, checkpoint_path):
            path.unlink(missing_ok=True)

    threads = load_jsonl(jsonl_path)
    completed_ids = {str(thread["thread_id"]) for thread in threads}
    # Always rebuild CSV at startup. This repairs a crash between the durable
    # JSONL append and the corresponding CSV append.
    write_csv(csv_path, threads)

    checkpoint = load_checkpoint(checkpoint_path)
    if checkpoint:
        if checkpoint.get("forum_url") != args.forum_url or checkpoint.get("keyword") != args.keyword:
            raise SystemExit(
                "This output directory belongs to a different forum URL or keyword. "
                "Use another --output-dir or pass --fresh to replace it."
            )
        page_url = checkpoint.get("current_forum_url")
        pages_completed = int(checkpoint.get("listing_pages_completed", 0))
        if checkpoint.get("status") == "complete" and page_url is None:
            print(
                f"Already complete: {len(threads)} thread(s). Use --fresh to start over.\n"
                f"JSONL: {jsonl_path}\nCSV:   {csv_path}\nCheckpoint: {checkpoint_path}"
            )
            return 0
    else:
        page_url = args.forum_url
        pages_completed = 0

    state = {
        "version": 1,
        "forum_url": args.forum_url,
        "keyword": args.keyword,
        "current_forum_url": page_url,
        "listing_pages_completed": pages_completed,
        "completed_thread_count": len(completed_ids),
        "status": "running",
        "last_error": None,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_checkpoint(checkpoint_path, state)

    scraper = ForumScraper(
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        backoff=args.backoff,
    )
    pages_seen_this_run: set[str] = set()
    exit_code = 0
    try:
        while page_url and page_url not in pages_seen_this_run:
            if args.forum_pages > 0 and pages_completed >= args.forum_pages:
                state["status"] = "page_limit_reached"
                break
            if args.max_threads and len(completed_ids) >= args.max_threads:
                state["status"] = "thread_limit_reached"
                break

            state["current_forum_url"] = page_url
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_checkpoint(checkpoint_path, state)
            pages_seen_this_run.add(page_url)
            soup = scraper.get_soup(page_url)
            scraper.validate_listing_page(soup, page_url)

            reached_thread_limit = False
            for link in scraper.listing_threads(soup, page_url):
                if args.keyword not in link.title or link.thread_id in completed_ids:
                    continue
                if args.max_threads and len(completed_ids) >= args.max_threads:
                    reached_thread_limit = True
                    break
                logging.info('MATCH [%s] "%s"', link.thread_id, link.title)
                thread = scraper.scrape_thread(link)
                append_thread_jsonl(jsonl_path, thread)
                append_thread_csv(csv_path, thread)
                threads.append(thread)
                completed_ids.add(link.thread_id)
                state["completed_thread_count"] = len(completed_ids)
                state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                save_checkpoint(checkpoint_path, state)

            if reached_thread_limit:
                state["status"] = "thread_limit_reached"
                break

            pages_completed += 1
            page_url = scraper.next_listing_url(soup, page_url)
            state["listing_pages_completed"] = pages_completed
            state["current_forum_url"] = page_url
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_checkpoint(checkpoint_path, state)

        if page_url is None:
            state["status"] = "complete"
        elif page_url in pages_seen_this_run and state["status"] == "running":
            raise RuntimeError(f"Listing pagination loop detected at {page_url}")
    except KeyboardInterrupt:
        state["status"] = "stopped_by_user"
        state["last_error"] = "KeyboardInterrupt"
        exit_code = 130
        logging.warning("Stopped by user; progress is saved and can be resumed.")
    except Exception as exc:
        state["status"] = "interrupted"
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        logging.error("Run interrupted: %s", exc)
    finally:
        state["current_forum_url"] = page_url
        state["listing_pages_completed"] = pages_completed
        state["completed_thread_count"] = len(completed_ids)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_checkpoint(checkpoint_path, state)

    print(
        f"Status: {state['status']}. Saved {len(threads)} matching thread(s), "
        f"{sum(1 + t['comment_count_scraped'] for t in threads if t['opening_post'])} post(s).\n"
        f"JSONL: {jsonl_path}\nCSV:   {csv_path}\nCheckpoint: {checkpoint_path}"
    )
    if exit_code:
        print("Run the same command again to resume from the checkpoint.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
