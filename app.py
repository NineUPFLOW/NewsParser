import html
import logging
import os
import re
import time
from datetime import datetime, timezone

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("digest")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
MESSAGE_THREAD_ID = int(os.environ.get("MESSAGE_THREAD_ID", "0"))

RSS_FEEDS = [
    "https://meduza.io/rss/all",
    "https://ru.themoscowtimes.com/rss/news",
    "https://novayagazeta.eu/rss",
    "https://thebell.io/rss",
    "https://istories.media/rss/all.xml",
    "https://ovd.info/rss",
    "https://newtimes.ru/rss/",
    "http://www.colta.ru/feed",
]

MAX_ARTICLES = 15
TITLE_MAX = 200
SUMMARY_MAX = 250


def fetch_feed(url):
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        log.warning("Feed failed %s: %s", url, e)
        return []

    items = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue

        dt = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                dt = datetime.fromtimestamp(time.mktime(entry[key]), tz=timezone.utc)
                break
        if dt is None:
            dt = datetime.now(timezone.utc)

        summary = entry.get("summary") or entry.get("description") or ""
        items.append({
            "title": title,
            "link": link,
            "dt": dt,
            "summary": summary,
        })
    return items


def clean_text(raw):
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_message(articles):
    parts = ["📰 <b>Дайджест новостей</b>\n"]
    for a in articles:
        title = html.escape(a["title"][:TITLE_MAX])
        summary = html.escape(clean_text(a["summary"])[:SUMMARY_MAX])
        dt = a["dt"].strftime("%d.%m %H:%M")
        parts.append(
            f"\n🔹 <b>{title}</b>\n"
            f"<i>{dt}</i>\n"
            f"{summary}\n"
            f'<a href="{a["link"]}">Читать</a>\n'
        )
    return "".join(parts)


def split_message(text, limit=4000):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > limit:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": MESSAGE_THREAD_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    all_items = []
    for url in RSS_FEEDS:
        items = fetch_feed(url)
        log.info("Feed %s -> %d", url, len(items))
        all_items.extend(items)

    seen, unique = set(), []
    for it in all_items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)

    unique.sort(key=lambda x: x["dt"], reverse=True)
    top = unique[:MAX_ARTICLES]
    log.info("Total unique: %d, sending top %d", len(unique), len(top))

    if not top:
        log.warning("Nothing to send")
        return

    text = format_message(top)
    chunks = split_message(text)
    for chunk in chunks:
        send_telegram(chunk)
    log.info("Sent %d message(s)", len(chunks))


if __name__ == "__main__":
    main()
