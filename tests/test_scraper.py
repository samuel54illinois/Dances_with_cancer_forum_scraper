from pathlib import Path

from bs4 import BeautifulSoup

from scraper import ForumScraper, clean_text


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
