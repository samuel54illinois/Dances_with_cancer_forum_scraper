#!/usr/bin/env python3
"""Scrape public yuaigongwu.com forum threads whose titles match a keyword."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


DEFAULT_FORUM_URL = "http://www.yuaigongwu.com/forum-145-279.html"
USER_AGENT = "YuaigongwuResearchScraper/1.0 (polite public-page research scraper)"
THREAD_ID_RE = re.compile(r"(?:thread-|[?&]tid=)(\d+)")
POST_ID_RE = re.compile(r"post_(\d+)$")
DATE_RE = re.compile(r"发表于\s*([^|]+)")


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
    def __init__(self, delay: float = 1.5, timeout: float = 30.0) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
        )
        self._last_request_at = 0.0

    def get_soup(self, url: str) -> BeautifulSoup:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.delay / 4)))

        logging.info("GET %s", url)
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.SSLError as exc:
            raise RuntimeError(
                "The site's HTTPS certificate could not be verified. "
                "Use its public HTTP URL (http://www.yuaigongwu.com/...) instead."
            ) from exc
        finally:
            self._last_request_at = time.monotonic()
        response.raise_for_status()
        # The server explicitly declares UTF-8. Statistical encoding detection
        # misidentifies these mostly-Chinese pages and produces mojibake.
        response.encoding = response.encoding or "utf-8"
        return BeautifulSoup(response.text, "html.parser")

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

    def matching_threads(
        self, forum_url: str, keyword: str, forum_pages: int
    ) -> Iterable[ThreadLink]:
        page_url: str | None = forum_url
        pages_seen: set[str] = set()
        thread_ids_seen: set[str] = set()
        pages_scraped = 0
        while page_url and page_url not in pages_seen:
            if forum_pages > 0 and pages_scraped >= forum_pages:
                break
            pages_seen.add(page_url)
            soup = self.get_soup(page_url)
            pages_scraped += 1
            for link in self.listing_threads(soup, page_url):
                if keyword in link.title and link.thread_id not in thread_ids_seen:
                    thread_ids_seen.add(link.thread_id)
                    yield link
            page_url = self.next_listing_url(soup, page_url)


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


def write_csv(path: Path, threads: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for thread in threads:
            posts = ([thread["opening_post"]] if thread["opening_post"] else []) + thread["comments"]
            for post in posts:
                writer.writerow(
                    {
                        "thread_id": thread["thread_id"],
                        "thread_title": thread["title"],
                        "thread_url": thread["url"],
                        **post,
                        "image_urls": json.dumps(post["image_urls"], ensure_ascii=False),
                        "link_urls": json.dumps(post["link_urls"], ensure_ascii=False),
                    }
                )


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
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.forum_pages < 0 or args.max_threads < 0:
        raise SystemExit("--forum-pages and --max-threads must be zero or positive")

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "threads.jsonl"
    csv_path = output_dir / "posts.csv"

    scraper = ForumScraper(delay=args.delay, timeout=args.timeout)
    links = scraper.matching_threads(args.forum_url, args.keyword, args.forum_pages)
    threads: list[dict] = []
    with jsonl_path.open("w", encoding="utf-8") as jsonl:
        for link in links:
            if args.max_threads and len(threads) >= args.max_threads:
                break
            logging.info('MATCH [%s] "%s"', link.thread_id, link.title)
            thread = scraper.scrape_thread(link)
            threads.append(thread)
            jsonl.write(json.dumps(thread, ensure_ascii=False) + "\n")
            jsonl.flush()

    write_csv(csv_path, threads)
    print(
        f"Done: {len(threads)} matching thread(s), "
        f"{sum(1 + t['comment_count_scraped'] for t in threads if t['opening_post'])} post(s).\n"
        f"JSONL: {jsonl_path}\nCSV:   {csv_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
