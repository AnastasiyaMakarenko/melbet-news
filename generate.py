"""Каждый запуск: берёт свежие спортивные новости из RSS, переписывает их через OpenRouter,
генерирует картинку без людей для каждой НОВОЙ новости и сохраняет всё в data/news.json."""
import base64, datetime, hashlib, html, json, os, pathlib, re

import feedparser
import requests

FEEDS = [f.strip() for f in os.getenv(
    "FEEDS", "https://www.sports.ru/rss/main.xml,https://www.championat.com/rss/news/"
).split(",") if f.strip()]
COUNT = 10
TEXT_MODEL = os.getenv("TEXT_MODEL", "openai/gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "openai/gpt-5-image-mini")
API = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
           "Content-Type": "application/json"}

DATA_DIR = pathlib.Path("data")
IMG_DIR = DATA_DIR / "img"
DATA_FILE = DATA_DIR / "news.json"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def ask(payload):
    r = requests.post(API, headers=HEADERS, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def fetch_entries():
    entries, seen = [], set()
    for url in FEEDS:
        feed = feedparser.parse(url)
        print(f"{url}: {len(feed.entries)} записей")
        for e in feed.entries:
            key = e.get("link") or e.get("title", "")
            nid = hashlib.md5(key.encode()).hexdigest()[:12]
            if not key or nid in seen:
                continue
            seen.add(nid)
            entries.append({"id": nid, "title": clean(e.get("title")),
                            "summary": clean(e.get("summary"))[:1500]})
            if len(entries) >= COUNT:
                return entries
    return entries


def rewrite(entry):
    msg = ask({
        "model": TEXT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Ты редактор спортивной новостной ленты. Перепиши новость своими словами на русском. "
                "Используй только факты из исходника, ничего не добавляй от себя. Верни только JSON с полями: "
                "title — заголовок до 90 символов; "
                "text — 2–3 предложения; "
                "image_prompt — на английском, описание атмосферной фотореалистичной иллюстрации к новости "
                "БЕЗ людей: стадион, поле, мяч, ворота, трибуны, экипировка, табло и т.п. "
                "Без людей, лиц, силуэтов, текста, логотипов и флагов.")},
            {"role": "user", "content": json.dumps(entry, ensure_ascii=False)},
        ],
    })
    text = re.sub(r"^```(json)?|```$", "", msg["content"].strip()).strip()
    return json.loads(text)


def make_image(prompt, nid):
    msg = ask({
        "model": IMAGE_MODEL,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content":
            "Generate a landscape 3:2 photorealistic image. " + prompt +
            " Absolutely no people, no faces, no silhouettes, no text, no logos."}],
    })
    url = msg["images"][0]["image_url"]["url"]          # data:image/png;base64,....
    head, b64 = url.split(",", 1)
    ext = "jpg" if "jpeg" in head or "jpg" in head else head.split("/")[1].split(";")[0]
    name = f"{nid}.{ext}"
    (IMG_DIR / name).write_bytes(base64.b64decode(b64))
    return f"img/{name}"


def main():
    old = {}
    if DATA_FILE.exists():
        old = {i["id"]: i for i in json.loads(DATA_FILE.read_text("utf-8")).get("items", [])}

    entries = fetch_entries()
    if not entries:
        raise SystemExit("RSS ничего не отдал — проверь ссылки в FEEDS")

    items = []
    for e in entries:
        if e["id"] in old:
            items.append(old[e["id"]])
            continue
        try:
            r = rewrite(e)
            item = {"id": e["id"], "title": r["title"], "text": r["text"], "image": None}
            try:
                item["image"] = make_image(r["image_prompt"], e["id"])
            except Exception as err:
                print("Картинка не сгенерилась:", err)
            items.append(item)
            print("Новая:", item["title"])
        except Exception as err:
            print("Новость пропущена:", err)

    DATA_FILE.write_text(json.dumps({
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }, ensure_ascii=False, indent=1), "utf-8")

    used = {pathlib.Path(i["image"]).name for i in items if i.get("image")}
    for f in IMG_DIR.iterdir():
        if f.name != ".gitkeep" and f.name not in used:
            f.unlink()


if __name__ == "__main__":
    main()
