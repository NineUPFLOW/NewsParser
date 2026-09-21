import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
MESSAGE_THREAD_ID = int(os.environ.get("MESSAGE_THREAD_ID", "0"))

STATE_FILE = Path("state.json")
MAX_POSTS_PER_RUN = 5
MAX_SEEN = 5000
TITLE_MAX = 200
SUMMARY_MAX = 500
CAPTION_LIMIT = 1000

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


# ---------- state ----------

def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
            data.setdefault("seen", [])
            data.setdefault("queue", [])
            return data
        except Exception as e:
            log.warning("State load failed: %s", e)
    return {"seen": [], "queue": []}


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ---------- parsing ----------

def clean_text(raw):
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url and url.startswith("http"):
                return url
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or ""
        typ = enc.get("type") or ""
        if href.startswith("http") and (typ.startswith("image/") or href.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp", ".gif"))):
            return href
    # Фолбэк: ищем <img src="..."> в summary
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    if m and m.group(1).startswith("http"):
        return m.group(1)
    return None


def fetch_feed(url):
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        log.warning("Feed failed %s: %s", url, e)
        return []

    items = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue

        dt = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                dt = datetime.fromtimestamp(time.mktime(e[key]), tz=timezone.utc)
                break
        if dt is None:
            dt = datetime.now(timezone.utc)

        items.append({
            "title": title[:TITLE_MAX],
            "link": link,
            "dt": dt.isoformat(),
            "summary": clean_text(e.get("summary") or e.get("description") or "")[:SUMMARY_MAX],
            "image": extract_image(e),
        })
    return items


# ---------- telegram ----------

def send_photo(image_url, caption):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": MESSAGE_THREAD_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        },
        timeout=30,
    )
    return r.ok, r.text


def send_text(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": MESSAGE_THREAD_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    return r.ok, r.text


def format_post(item):
    title = html.escape(item["title"])
    summary = html.escape(item["summary"])
    # Заголовок — кликабельная ссылка, без отдельной "Читать полностью"
    body = f'<a href="{item["link"]}"><b>{title}</b></a>'
    if summary:
        body += f"\n\n{summary}"
    if len(body) > CAPTION_LIMIT:
        body = body[:CAPTION_LIMIT - 1] + "…"
    return body


def publish(item):
    caption = format_post(item)
    if item.get("image"):
        ok, resp = send_photo(item["image"], caption)
        if ok:
            return True
        log.warning("sendPhoto failed, fallback to text: %s", resp[:200])
    ok, resp = send_text(caption)
    if not ok:
        log.error("sendMessage failed: %s", resp[:300])
    return ok


# ---------- main ----------

def main():
    state = load_state()
    seen = set(state["seen"])
    queue = state["queue"]

    # 1. Собираем свежие новости из RSS
    fresh = []
    for url in RSS_FEEDS:
        items = fetch_feed(url)
        log.info("Feed %s -> %d", url, len(items))
        fresh.extend(items)

    # 2. Дедуп внутри партии + отбрасываем уже виденное и то, что уже в очереди
    queue_links = {it["link"] for it in queue}
    new_unique = []
    batch_seen = set()
    for it in fresh:
        if it["link"] in seen or it["link"] in queue_links or it["link"] in batch_seen:
            continue
        batch_seen.add(it["link"])
        new_unique.append(it)

    # Свежие — в конец очереди, сортируем по дате
    new_unique.sort(key=lambda x: x["dt"])
    queue.extend(new_unique)
    log.info("New items: %d, queue size before send: %d", len(new_unique), len(queue))

    # 3. Отправляем до 5 постов: сначала из очереди (старое), потом ничего
    sent_links = []
    sent_count = 0
    while queue and sent_count < MAX_POSTS_PER_RUN:
        item = queue[0]
        if item["link"] in seen:
            queue.pop(0)
            continue
        if publish(item):
            sent_links.append(item["link"])
            seen.add(item["link"])
            queue.pop(0)
            sent_count += 1
        else:
            # не смогли — оставляем в очереди, но не зацикливаемся
            log.warning("Publish failed, keeping in queue: %s", item["link"])
            break

    # 4. Сохраняем состояние
    state["seen"].extend(sent_links)
    state["queue"] = queue
    save_state(state)
    log.info("Sent: %d, queue after: %d", sent_count, len(queue))


if __name__ == "__main__":
    main()
