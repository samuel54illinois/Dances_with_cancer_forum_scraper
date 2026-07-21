import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper import (
    ForumScraper,
    ThreadLink,
    UnexpectedPageError,
    clean_text,
    failure_record,
    main,
    load_checkpoint,
    load_jsonl,
    save_checkpoint,
    save_failure_logs,
)


def test_listing_extracts_normal_threads_and_ignores_sidebar():
    soup = BeautifulSoup(
        """
        <a href="thread-999-1-1.html">肝癌 sidebar</a>
        <table id="threadlisttableid">
          <tbody id="normalthread_231">
            <a href="thread-231-1-279.html" onclick="atarget(this)">原发性肝癌求助</a>
            <a href="thread-231-2-279.html" onclick="atarget(this)">2</a>
          </tbody>
        </table>
        """,
        "html.parser",
    )
    links = ForumScraper(delay=0).listing_threads(soup, "http://example.test/forum.html")
    assert [(x.thread_id, x.title) for x in links] == [("231", "原发性肝癌求助")]
    assert links[0].url == "http://example.test/thread-231-1-1.html"


def test_parse_posts_separates_opening_post_and_reply():
    fixture = Path(__file__).with_name("thread_fragment.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(fixture, "html.parser")
    posts = ForumScraper(delay=0).parse_posts(soup, "http://example.test/thread.html", 1)
    assert len(posts) == 2
    assert posts[0]["is_opening_post"] is True
    assert posts[0]["author"] == "楼主甲"
    assert posts[0]["posted_at"] == "2012-07-23 17:19:39"
    assert posts[0]["content"] == "主题：\n第一行\n第二行"
    assert posts[1]["is_opening_post"] is False
    assert posts[1]["author"] == "回复乙"
    assert posts[1]["content"] == "谢谢！"


def test_clean_text_removes_login_notice():
    node = BeautifulSoup(
        '<td><div class="attach_nopermission">请登录</div>真实内容<br>下一行</td>',
        "html.parser",
    ).td
    assert clean_text(node) == "真实内容\n下一行"


def test_load_jsonl_repairs_truncated_final_line(tmp_path):
    path = tmp_path / "threads.jsonl"
    thread = {"thread_id": "231", "title": "肝癌"}
    path.write_text(json.dumps(thread, ensure_ascii=False) + '\n{"thread_id":', encoding="utf-8")

    assert load_jsonl(path) == [thread]
    assert path.read_text(encoding="utf-8") == json.dumps(thread, ensure_ascii=False) + "\n"


def test_checkpoint_round_trip_is_atomic(tmp_path):
    path = tmp_path / "checkpoint.json"
    state = {"status": "running", "completed_thread_count": 95}
    save_checkpoint(path, state)
    assert load_checkpoint(path) == state
    assert not (tmp_path / "checkpoint.json.tmp").exists()


def test_redirect_to_network_filter_is_rejected():
    with pytest.raises(UnexpectedPageError, match="filtering/block page"):
        ForumScraper._validate_destination(
            "http://www.yuaigongwu.com/forum-145-1.html",
            "http://10.12.55.2/disable/disable.htm",
        )


def test_thread_validation_rejects_skeleton_without_messages():
    soup = BeautifulSoup(
        '<span id="thread_subject">肝癌</span><div id="postlist"></div>',
        "html.parser",
    )
    with pytest.raises(UnexpectedPageError, match="Expected a thread"):
        ForumScraper.validate_thread_page(soup, "http://example.test/thread-1.html")


def test_failed_thread_is_logged_and_next_thread_is_saved(tmp_path, monkeypatch):
    listing = BeautifulSoup(
        """
        <table id="threadlisttableid">
          <tbody id="normalthread_1">
            <a href="thread-1-1-1.html" onclick="atarget(this)">肝癌 empty</a>
          </tbody>
          <tbody id="normalthread_2">
            <a href="thread-2-1-1.html" onclick="atarget(this)">肝癌 valid</a>
          </tbody>
        </table>
        """,
        "html.parser",
    )

    def fake_get_soup(self, url, expected=None):
        return listing

    def fake_scrape_thread(self, link):
        if link.thread_id == "1":
            raise RuntimeError("blank thread after retries")
        opening = {
            "post_id": "20",
            "is_opening_post": True,
            "floor": "楼主",
            "author": "作者",
            "posted_at": "2020-01-01 00:00:00",
            "content": "正文",
            "image_urls": [],
            "link_urls": [],
            "thread_page": 1,
            "source_url": link.url,
        }
        return {
            "thread_id": link.thread_id,
            "title": link.title,
            "url": link.url,
            "source_forum_url": link.source_forum_url,
            "theme": "正文",
            "opening_post": opening,
            "comments": [],
            "comment_count_scraped": 0,
            "thread_pages_scraped": 1,
        }

    monkeypatch.setattr(ForumScraper, "get_soup", fake_get_soup)
    monkeypatch.setattr(ForumScraper, "scrape_thread", fake_scrape_thread)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scraper.py",
            "--forum-url",
            "http://example.test/forum-1.html",
            "--forum-pages",
            "0",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    saved = load_jsonl(tmp_path / "threads.jsonl")
    failures = [
        json.loads(line)
        for line in (tmp_path / "failed_threads.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    checkpoint = load_checkpoint(tmp_path / "checkpoint.json")
    assert [item["thread_id"] for item in saved] == ["2"]
    assert [item["thread_id"] for item in failures] == ["1"]
    assert checkpoint["status"] == "complete_with_failures"
    assert checkpoint["completed_thread_count"] == 1
    assert checkpoint["failed_thread_count"] == 1


def test_retry_failures_recovers_thread_and_clears_log(tmp_path, monkeypatch):
    forum_url = "http://example.test/forum-1.html"
    link = ThreadLink("7", "肝癌 recovered", "http://example.test/thread-7.html", forum_url)
    failure = failure_record(link, "previous blank page")
    save_failure_logs(
        tmp_path / "failed_threads.jsonl",
        tmp_path / "failed_threads.csv",
        {"7": failure},
    )
    save_checkpoint(
        tmp_path / "checkpoint.json",
        {
            "version": 1,
            "forum_url": forum_url,
            "keyword": "肝癌",
            "current_forum_url": forum_url,
            "listing_pages_completed": 0,
            "completed_thread_count": 0,
            "failed_thread_count": 1,
            "status": "interrupted",
        },
    )

    def fake_scrape_thread(self, retry_link):
        opening = {
            "post_id": "70",
            "is_opening_post": True,
            "floor": "楼主",
            "author": "作者",
            "posted_at": "2020-01-01 00:00:00",
            "content": "恢复正文",
            "image_urls": [],
            "link_urls": [],
            "thread_page": 1,
            "source_url": retry_link.url,
        }
        return {
            "thread_id": retry_link.thread_id,
            "title": retry_link.title,
            "url": retry_link.url,
            "source_forum_url": retry_link.source_forum_url,
            "theme": "恢复正文",
            "opening_post": opening,
            "comments": [],
            "comment_count_scraped": 0,
            "thread_pages_scraped": 1,
        }

    monkeypatch.setattr(ForumScraper, "scrape_thread", fake_scrape_thread)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scraper.py",
            "--forum-url",
            forum_url,
            "--output-dir",
            str(tmp_path),
            "--retry-failures",
        ],
    )

    assert main() == 0
    assert [item["thread_id"] for item in load_jsonl(tmp_path / "threads.jsonl")] == ["7"]
    assert (tmp_path / "failed_threads.jsonl").read_text(encoding="utf-8") == ""
    checkpoint = load_checkpoint(tmp_path / "checkpoint.json")
    assert checkpoint["completed_thread_count"] == 1
    assert checkpoint["failed_thread_count"] == 0
