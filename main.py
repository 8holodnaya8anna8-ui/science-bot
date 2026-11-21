
import json
import os
import re
import time
import html
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---- Настройки (можно менять) -------------------------------------------
FEEDS = [
    ("ScienceDaily · Психология", "https://www.sciencedaily.com/rss/mind_brain/psychology.xml"),
    ("ScienceDaily · Нейронаука", "https://www.sciencedaily.com/rss/mind_brain/neuroscience.xml"),
    ("PsyPost", "https://www.psypost.org/feed/"),
]
HOURS_BACK = 49        # за какой период брать статьи (рассылка раз в 2 дня)
PER_FEED = 4           # максимум статей из одной ленты
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# --------------------------------------------------------------------------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (science-digest-bot)"}


def http(url, data=None, headers=None, tries=5):
    """Запрос с повторными попытками: иногда соединение обрывается случайно."""
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:  # ошибка в данных, повтор не поможет
                print(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")
                raise
            last = e
        except Exception as e:
            last = e
        print(f"Попытка {n + 1} не удалась: {type(last).__name__}")
        time.sleep(3 * (n + 1) ** 2)  # 3, 12, 27, 48 секунд
    raise last


def clean(text, limit=500):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def parse_feed(xml_bytes, source):
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"), 300)
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            when = None
        out.append({"source": source, "title": title, "link": link,
                    "summary": clean(it.findtext("description")), "when": when})
    return out


def collect():
    since = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    seen, result = set(), []
    for source, url in FEEDS:
        try:
            items = parse_feed(http(url), source)
        except Exception as e:
            print(f"Лента не открылась ({source}): {e}")
            continue
        fresh = [i for i in items if i["when"] is None or i["when"] >= since]
        fresh.sort(key=lambda i: i["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for i in fresh[:PER_FEED]:
            if i["link"] not in seen:
                seen.add(i["link"])
                result.append(i)
    return result


def translate(items):
    """Возвращает список {"title","summary"} на русском или None при сбое."""
    if not GEMINI_KEY or not items:
        return None
    articles = [{"title": i["title"], "description": i["summary"]} for i in items]
    prompt = (
        "Для каждой научной статьи верни по-русски: "
        "title — переведённый заголовок; "
        "summary — краткое изложение в 1–2 предложениях строго по данному описанию, "
        "ничего не добавляя и не преувеличивая выводы. "
        "Верни только JSON-массив объектов {\"title\": ..., \"summary\": ...} "
        "в том же порядке и в том же количестве.\n\n"
        + json.dumps(articles, ensure_ascii=False)
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={urllib.parse.quote(GEMINI_KEY)}")
    try:
        raw = http(url, data=body, headers={"Content-Type": "application/json"})
        text = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        if isinstance(data, list) and len(data) == len(items):
            return data
    except Exception as e:
        print(f"Перевод не получился: {type(e).__name__}")
    return None


def build_message(items, ru):
    blocks = ["Наука и психология: новое за последние дни"]
    for n, it in enumerate(items, 1):
        if ru:
            r = ru[n - 1]
            blocks.append(f"{n}. {r.get('title', it['title'])}\n{r.get('summary', '')}\n"
                          f"{it['source']} · {it['link']}")
        else:
            blocks.append(f"{n}. {it['title']}\n{it['source']} · {it['link']}")
    if not ru:
        blocks.append("(Перевод сейчас недоступен, показаны оригинальные заголовки.)")
    return blocks


def send(blocks):
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        chunks.append(cur)
    for c in chunks:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": c,
                                       "disable_web_page_preview": "true"}).encode()
        http(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data,
             headers={"Content-Type": "application/x-www-form-urlencoded"})


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    items = collect()
    if not items:
        send(["Наука и психология: за последние два дня новых статей не нашлось."])
        return
    send(build_message(items, translate(items)))
    print(f"Отправлено статей: {len(items)}")


if __name__ == "__main__":
    main()
