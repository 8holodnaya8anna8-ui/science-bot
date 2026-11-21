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
# Язык подборки. Варианты: "русском", "украинском", "английском"
LANGUAGE = "русском"

# Источники: (раздел, название источника, адрес RSS-ленты).
# Чтобы добавить источник, допишите ещё одну строку. Чтобы убрать, удалите строку.
FEEDS = [
    ("Психология и мозг", "ScienceDaily · Психология", "https://www.sciencedaily.com/rss/mind_brain/psychology.xml"),
    ("Психология и мозг", "ScienceDaily · Нейронаука", "https://www.sciencedaily.com/rss/mind_brain/neuroscience.xml"),
    ("Психология и мозг", "PsyPost", "https://www.psypost.org/feed/"),
    ("Наука в мире", "ScienceDaily · Главное", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("Наука в мире", "Science (AAAS)", "https://www.science.org/rss/news_current.xml"),
    ("Наука в мире", "Phys.org", "https://phys.org/rss-feed/"),
    ("Наука в мире", "ScienceDaily · Здоровье и медицина", "https://www.sciencedaily.com/rss/health_medicine.xml"),
    ("Наука в мире", "ScienceDaily · Космос", "https://www.sciencedaily.com/rss/space_time.xml"),
    ("Наука в мире", "ScienceDaily · Земля и климат", "https://www.sciencedaily.com/rss/earth_climate.xml"),
    ("Наука в мире", "BBC · Наука", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("Наука в мире", "The Guardian · Наука", "https://www.theguardian.com/science/rss"),
]
HOURS_BACK = 49        # за какой период брать статьи (рассылка раз в 2 дня)
PER_FEED = 2           # максимум статей из одной ленты
MAX_PER_GROUP = 8      # максимум статей в одном разделе
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# --------------------------------------------------------------------------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (science-digest-bot)"}


def http(url, data=None, headers=None, tries=5, timeout=30):
    """Запрос с повторными попытками: иногда соединение обрывается случайно."""
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:  # ошибка в данных, повтор не поможет
                print(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")
                raise
            last = e
        except Exception as e:
            last = e
        print(f"Попытка {n + 1} не удалась: {type(last).__name__} {getattr(last, 'code', '')}")
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
    """Возвращает [(раздел, [статьи])], в каждом разделе источники чередуются."""
    since = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    groups = {}
    for group, source, url in FEEDS:
        try:
            items = parse_feed(http(url, tries=3), source)
        except Exception as e:
            print(f"Лента не открылась ({source}): {type(e).__name__}")
            continue
        fresh = [i for i in items if i["when"] is None or i["when"] >= since]
        fresh.sort(key=lambda i: i["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        groups.setdefault(group, []).append(fresh[:PER_FEED])
    seen, result = set(), []
    for group, lists in groups.items():
        picked = []
        for k in range(PER_FEED):
            for lst in lists:
                if k < len(lst) and lst[k]["link"] not in seen:
                    seen.add(lst[k]["link"])
                    picked.append(lst[k])
        result.append((group, picked[:MAX_PER_GROUP]))
    return result


def translate(items):
    """Возвращает список {"title","summary"} на нужном языке или None при сбое."""
    if not GEMINI_KEY or not items:
        return None
    articles = [{"title": i["title"], "description": i["summary"]} for i in items]
    prompt = (
        f"Для каждой научной статьи верни на {LANGUAGE} языке: "
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
    models = [GEMINI_MODEL] + [m for m in ("gemini-flash-lite-latest",) if m != GEMINI_MODEL]
    for model in models:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={urllib.parse.quote(GEMINI_KEY)}")
        try:
            raw = http(url, data=body, headers={"Content-Type": "application/json"},
                       tries=3, timeout=120)
            text = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            if isinstance(data, list) and len(data) == len(items):
                return data
            print(f"Перевод вернул неполный ответ ({model})")
        except Exception as e:
            print(f"Перевод не получился ({model}): {type(e).__name__}")
    return None


def item_blocks(items, ru, start):
    blocks = []
    for n, it in enumerate(items, start):
        if ru:
            r = ru[n - start]
            blocks.append(f"{n}. {r.get('title', it['title'])}\n{r.get('summary', '')}\n"
                          f"{it['source']} · {it['link']}")
        else:
            blocks.append(f"{n}. {it['title']}\n{it['source']} · {it['link']}")
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
    groups = [(g, items) for g, items in collect() if items]
    if not groups:
        send(["Наука и психология: за последние два дня новых статей не нашлось."])
        return
    blocks = ["Наука и психология: новое за последние дни"]
    n, translated_all = 1, True
    for group, items in groups:
        ru = translate(items)
        translated_all = translated_all and bool(ru)
        blocks.append(f"— {group} —")
        blocks += item_blocks(items, ru, n)
        n += len(items)
    if not translated_all:
        blocks.append("(Перевод сейчас недоступен для части статей, показаны оригинальные заголовки.)")
    send(blocks)
    print(f"Отправлено статей: {n - 1}")


if __name__ == "__main__":
    main()
