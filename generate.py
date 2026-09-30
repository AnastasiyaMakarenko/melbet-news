"""Каждый запуск: берёт свежие спортивные новости из RSS, для каждой НОВОЙ забирает текст статьи с сайта-источника,
переписывает его через OpenRouter (заголовок, лид, полный текст, раздел), генерирует картинку без людей
и добавляет новость в начало ленты. Лента копится (архив до KEEP новостей).

Что получается в data/:
  news.json      — лента: updated_at + items[{id,title,lead,category,published,image,thumb}], новые сверху
  a/<id>.json    — полная статья: те же поля + body (список абзацев)
  img/<id>.jpg   — картинка (до 1024px), img/<id>_s.jpg — миниатюра для ленты
"""
import base64, calendar, datetime, hashlib, html, io, json, os, pathlib, re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import feedparser
import requests
from PIL import Image

FEEDS = [f.strip() for f in os.getenv(
    "FEEDS", "https://www.sports.ru/rss/main.xml,https://www.championat.com/rss/news/"
).split(",") if f.strip()]
COUNT = 10      # сколько самых свежих записей из RSS смотреть за запуск
KEEP = 150      # сколько новостей хранить в ленте (старые удаляются вместе с картинками)
WORKERS = 4     # сколько новостей обрабатывать параллельно
CATEGORIES = ["Футбол", "Хоккей", "Баскетбол", "Теннис", "Единоборства", "Автоспорт", "Киберспорт", "Другое"]
TEXT_MODEL = os.getenv("TEXT_MODEL", "openai/gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "openai/gpt-5-image-mini")
# рекламные/промо-записи, которые попадают в RSS
AD_WORDS = re.compile(r"приглаша|получите|призы за|промокод|бонус|розыгрыш|реклам", re.I)
# спецпроекты, нативная реклама и не-спортивные разделы (лайфстайл, здоровье и т.п.)
AD_LINKS = re.compile(r"/special/|/promo|utm_|/lifestyle/|/health/|/life/|/stars/", re.I)
# спонсоры-букмекеры в названиях турниров («Фонбет КХЛ» -> «КХЛ»): на сайте Melbet конкурентов не упоминаем
SPONSORS = re.compile(r"\b(?:Фонбет|Fonbet|FONBET|Winline|Винлайн|Лига Ставок|Лиги Ставок|Лигой Ставок|Бетсити|BetCity|"
                      r"BetBoom|Бетбум|Олимпбет|OLIMPBET|Olimpbet|Марафонбет|Альфа-Банк)\s*")
API = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
           "Content-Type": "application/json"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}

DATA_DIR = pathlib.Path("data")
IMG_DIR = DATA_DIR / "img"
ART_DIR = DATA_DIR / "a"
INDEX = DATA_DIR / "news.json"
IMG_DIR.mkdir(parents=True, exist_ok=True)
ART_DIR.mkdir(parents=True, exist_ok=True)


def ask(payload):
    r = requests.post(API, headers=HEADERS, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def domain(url):
    host = urlparse(url).netloc.lower()
    return ".".join(host.split(".")[-2:])


def published(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    now = datetime.datetime.now(datetime.timezone.utc)
    dt = datetime.datetime.fromtimestamp(calendar.timegm(t), datetime.timezone.utc) if t else now
    return min(dt, now).isoformat(timespec="seconds")


def fetch_entries():
    """Берёт новости из всех лент по очереди (1-я из первой, 1-я из второй, ...), отсеивает рекламу."""
    per_feed = []
    for url in FEEDS:
        feed = feedparser.parse(url)
        good = []
        for e in feed.entries:
            link, title = e.get("link", ""), clean(e.get("title"))
            if not link or domain(link) != domain(url) or AD_WORDS.search(title) or AD_LINKS.search(link):
                print("  пропуск (реклама/чужой сайт):", link, "|", title)
                continue
            good.append({"id": hashlib.md5(link.encode()).hexdigest()[:12], "title": title,
                         "summary": clean(e.get("summary"))[:1500], "link": link, "published": published(e)})
        print(f"{url}: {len(feed.entries)} записей, годных {len(good)}")
        per_feed.append(good)

    entries, seen = [], set()
    for i in range(max(map(len, per_feed), default=0)):
        for good in per_feed:
            if i < len(good) and good[i]["id"] not in seen:
                seen.add(good[i]["id"])
                entries.append(good[i])
                if len(entries) >= COUNT:
                    return entries
    return entries


def page_text(url):
    """Текст статьи со страницы источника: все абзацы <p> подряд. Не получилось — пустая строка."""
    try:
        r = requests.get(url, headers=BROWSER_UA, timeout=30)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
        paras = [clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
        return "\n".join(p for p in paras if len(p) >= 60)[:6000]
    except Exception as err:
        print("  не удалось скачать статью:", url, err)
        return ""


def rewrite(entry, source_text):
    msg = ask({
        "model": TEXT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Ты редактор спортивного новостного сайта. Перепиши новость своими словами на русском, "
                "нейтрально и без кликбейта. Используй только факты из исходника, ничего не выдумывай. "
                "В тексте страницы может быть мусор с сайта (другие новости, реклама, комментарии) — "
                "бери только то, что относится к новости с данным заголовком. Если фактов мало — пиши короче. "
                "Не упоминай букмекеров и спонсоров в названиях турниров и лиг: пиши просто «КХЛ», «РПЛ», "
                "«Суперлига», «Кубок России». "
                "Верни только JSON с полями: "
                "title — заголовок до 90 символов; "
                "lead — 1–2 предложения, суть новости; "
                "body — массив из 2–6 абзацев полного текста статьи (лид в нём не повторяй); "
                f"category — ровно одно из: {', '.join(CATEGORIES)}; "
                "image_prompt — на английском, описание атмосферной фотореалистичной иллюстрации к новости "
                "БЕЗ людей: стадион, поле, мяч, ворота, трибуны, экипировка, табло и т.п. "
                "Без людей, лиц, силуэтов, текста, логотипов и флагов.")},
            {"role": "user", "content": json.dumps({
                "title": entry["title"], "summary": entry["summary"], "page_text": source_text,
            }, ensure_ascii=False)},
        ],
    })
    r = json.loads(re.sub(r"^```(json)?|```$", "", msg["content"].strip()).strip())
    body = r.get("body") or []
    if isinstance(body, str):
        body = [p for p in body.split("\n") if p.strip()]
    fix = lambda t: SPONSORS.sub("", str(t)).strip()
    return {
        "title": fix(r["title"]),
        "lead": fix(r.get("lead") or ""),
        "body": [fix(p) for p in body if str(p).strip()],
        "category": r.get("category") if r.get("category") in CATEGORIES else "Другое",
        "image_prompt": str(r.get("image_prompt") or entry["title"]),
    }


def save_images(img, nid):
    """Сохраняет большую картинку и миниатюру в JPEG (вместо PNG по 2 МБ)."""
    img = img.convert("RGB")
    big = img.copy(); big.thumbnail((1024, 1024))
    big.save(IMG_DIR / f"{nid}.jpg", "JPEG", quality=80, optimize=True, progressive=True)
    small = img.copy(); small.thumbnail((480, 480))
    small.save(IMG_DIR / f"{nid}_s.jpg", "JPEG", quality=75, optimize=True, progressive=True)
    return f"img/{nid}.jpg", f"img/{nid}_s.jpg"


def make_image(prompt, nid):
    big = IMG_DIR / f"{nid}.jpg"
    if big.exists():                                      # картинка уже есть — не платим второй раз
        return save_images(Image.open(big), nid) if not (IMG_DIR / f"{nid}_s.jpg").exists() \
            else (f"img/{nid}.jpg", f"img/{nid}_s.jpg")
    last = None
    for attempt in range(3):                              # модель иногда отвечает без картинки — пробуем ещё
        try:
            msg = ask({
                "model": IMAGE_MODEL,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content":
                    "Generate a landscape 3:2 photorealistic image. " + prompt +
                    " Absolutely no people, no faces, no silhouettes, no text, no logos."}],
            })
            url = msg["images"][0]["image_url"]["url"]  # data:image/png;base64,....
            return save_images(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))), nid)
        except Exception as err:
            last = err
            print(f"  картинка, попытка {attempt + 1}: {err!r}")
    raise last


def process(e):
    try:
        r = rewrite(e, page_text(e["link"]))
        art = {"id": e["id"], "title": r["title"], "lead": r["lead"], "body": r["body"],
               "category": r["category"], "published": e["published"], "image": None, "thumb": None}
        try:
            art["image"], art["thumb"] = make_image(r["image_prompt"], e["id"])
        except Exception as err:
            print("Картинка не сгенерилась:", err)
        print(f"Новая [{art['category']}]: {art['title']} | {e['link']}")
        return art
    except Exception as err:
        print("Новость пропущена:", e["link"], err)
        return None


def main():
    index = []
    if INDEX.exists():
        # новости в старом формате (без раздела) пересобираются заново, картинки при этом переиспользуются
        index = [i for i in json.loads(INDEX.read_text("utf-8")).get("items", []) if i.get("category")]
    known = {i["id"] for i in index}

    entries = fetch_entries()
    if not entries:
        raise SystemExit("RSS ничего не отдал — проверь ссылки в FEEDS")
    new = [e for e in entries if e["id"] not in known]
    print(f"Новых: {len(new)}")

    with ThreadPoolExecutor(WORKERS) as ex:
        for art in ex.map(process, new):
            if not art:
                continue
            (ART_DIR / f"{art['id']}.json").write_text(json.dumps(art, ensure_ascii=False, indent=1), "utf-8")
            index.append({k: art[k] for k in ("id", "title", "lead", "category", "published", "image", "thumb")})

    index.sort(key=lambda i: i["published"], reverse=True)
    index = index[:KEEP]
    INDEX.write_text(json.dumps({
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "items": index,
    }, ensure_ascii=False, indent=1), "utf-8")

    # удаляем картинки и статьи, которые выпали из ленты
    ids = {i["id"] for i in index}
    for f in list(IMG_DIR.iterdir()) + list(ART_DIR.iterdir()):
        if f.name != ".gitkeep" and f.stem.removesuffix("_s") not in ids:
            f.unlink()


if __name__ == "__main__":
    main()
