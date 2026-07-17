# Dances with Cancer forum scraper

This scraper reads public pages from the 与癌共舞 (Yuaigongwu) Discuz forum. It:

1. scans real thread rows on a forum listing page;
2. keeps titles containing a keyword (default: `肝癌`);
3. visits every page of each matching thread;
4. extracts the thread title, opening post (`theme`), and all comments;
5. writes nested JSONL and one-post-per-row CSV.

No browser clicking or scrolling is required because the public content is present in the HTML.

## Setup

Python 3.10+ is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

Scrape only the listing page supplied in the question (page 279):

```bash
python scraper.py
```

Test with at most one matching thread and no intentional delay:

```bash
python scraper.py --max-threads 1 --delay 0
```

Scan from the first listing page through every available listing page:

```bash
python scraper.py \
  --forum-url http://www.yuaigongwu.com/forum-145-1.html \
  --forum-pages 0 \
  --delay 2
```

Useful options:

- `--keyword 肝癌`: title substring to match
- `--forum-pages N`: listing pages to scan; `0` follows all remaining pages
- `--max-threads N`: stop after N matches; `0` means unlimited
- `--delay 1.5`: minimum delay between requests
- `--output-dir output`: destination directory

Results:

- `output/threads.jsonl`: one nested object per thread, including `theme`, `opening_post`, and `comments`
- `output/posts.csv`: one row per opening post/comment; encoded as UTF-8 with BOM for Excel

## Data fields

Each post includes its post ID, opening-post flag, floor label, author, timestamp, text,
image URLs, link URLs, thread page number, and source URL. The `comments` array excludes
the opening post. `theme` is the cleaned text of the opening post.

## Responsible use

The site's `robots.txt` permits the public forum/thread paths used here, but policies can
change. Recheck it before a large run. Keep a delay, avoid parallel requests, collect only
what your research requires, and handle usernames and health-related text as sensitive data.
The site currently serves its public pages over HTTP while its HTTPS certificate fails normal
verification; the default URL therefore uses HTTP and the scraper never disables TLS checks.
