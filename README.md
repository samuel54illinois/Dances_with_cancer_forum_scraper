# Dances with Cancer forum scraper

This scraper reads public pages from the 与癌共舞 (Yuaigongwu) Discuz forum. It:

1. scans real thread rows on a forum listing page;
2. keeps titles containing a keyword (default: `肝癌`);
3. visits every page of each matching thread;
4. extracts the thread title, opening post (`theme`), and all comments;
5. writes nested JSONL and one-post-per-row CSV after every completed thread;
6. resumes interrupted runs using an atomic checkpoint;
7. retries blank/temporary thread responses, logs persistent failures, and continues.

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

Test with at most one matching thread:

```bash
python scraper.py --max-threads 1 --delay 2 --output-dir output-smoke
```

Scan from the first listing page through every available listing page:

```bash
python scraper.py \
  --forum-url http://www.yuaigongwu.com/forum-145-1.html \
  --forum-pages 0 \
  --delay 2 \
  --output-dir output-full
```

### Stop and resume

Press `Ctrl+C` once to stop. The current completed thread, JSONL, CSV, and checkpoint are
preserved. To continue, run the **same command with the same output directory**. Resume is
automatic; already completed thread IDs are skipped.

For the current partial full-forum run:

```bash
python scraper.py \
  --forum-url http://www.yuaigongwu.com/forum-145-1.html \
  --forum-pages 0 \
  --max-threads 0 \
  --delay 2 \
  --output-dir output-full
```

If the process crashes or the network fails, run that command again. A thread is checkpointed
only after all of its reply pages have been collected, so at most the in-progress thread is
fetched again.

### Failed and empty threads

If a thread remains blank or invalid after all retries, the scraper writes it to both
`failed_threads.csv` and `failed_threads.jsonl`, then continues with the next matching thread.
It is not counted as successfully completed and does not create an empty record in
`threads.jsonl` or `posts.csv`.

Retry only unresolved failed threads later, without rescanning forum listings:

```bash
python scraper.py \
  --forum-url http://www.yuaigongwu.com/forum-145-1.html \
  --forum-pages 0 \
  --delay 3 \
  --timeout 45 \
  --output-dir output-full \
  --retry-failures
```

Confirmed-empty threads are intentionally skipped by that command. To explicitly check them
again too, add `--retry-empty`. A recovered thread is appended to the valid outputs and removed
from the current failure logs automatically.

Use `--fresh` only when you intentionally want to delete that output directory's scraper
results and begin again:

```bash
python scraper.py --output-dir output-smoke --fresh
```

Useful options:

- `--keyword 肝癌`: title substring to match
- `--forum-pages N`: listing pages to scan; `0` follows all remaining pages
- `--max-threads N`: stop after N matches; `0` means unlimited
- `--delay 1.5`: minimum delay between requests
- `--timeout 30`: seconds to wait for a response
- `--retries 5`: retries for timeouts, HTTP 429, and temporary server errors
- `--backoff 3`: initial retry pause; subsequent pauses increase exponentially
- `--output-dir output`: destination directory
- `--retry-failures`: retry only unresolved entries in the failure log
- `--retry-empty`: also retry records marked `confirmed_empty`
- `--fresh`: explicitly discard that destination's saved run and restart

Results:

- `output/threads.jsonl`: one nested object per thread, including `theme`, `opening_post`, and `comments`
- `output/posts.csv`: one row per opening post/comment; encoded as UTF-8 with BOM for Excel
- `output/checkpoint.json`: current listing URL, progress counts, status, and last error
- `output/failed_threads.csv`: human-readable unresolved/confirmed-empty thread log
- `output/failed_threads.jsonl`: machine-readable version of the same failure log

JSONL is the durable source of truth. At startup, CSV is rebuilt from valid JSONL records,
which repairs a crash that happened between writing the two formats. A truncated final JSONL
line is also removed automatically without affecting earlier completed threads.

## Throttling and retries

Keep a delay for a full scrape. Two seconds is a reasonable starting point for this older
forum; three to five seconds is gentler if the server is unstable. Removing the throttle makes
timeouts or rate limiting more likely and puts unnecessary load on the site. Requests remain
serial—there is no parallel fetching.

Temporary timeouts, blank/skeleton pages, HTTP 429 responses, and HTTP 5xx responses are retried
with exponential backoff. If retries for one thread are exhausted, that thread is logged and
the listing crawl continues. A listing-page failure still pauses the run because skipping a
listing page could silently omit many threads. Responses redirected to a network-filtering host
are rejected instead of being mistaken for an empty forum page.

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
