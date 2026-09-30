"""Каждый запуск (раз в 10 минут):
1. собирает свежие заголовки (не старше MAX_AGE_H часов) с 8 спортивных сайтов — RSS или страница новостей;
2. нейронка-редактор склеивает одно событие с разных сайтов в одну историю, выкидывает рекламу/ставки/не-спорт
   и то, что у нас уже опубликовано;
3. берёт до MAX_NEW историй — в первую очередь те, о которых пишут несколько сайтов (подтверждённые);
4. скачивает тексты статей из всех источников истории и пишет по ним уникальный текст (рерайт);
5. нейронка-фактчекер сверяет рерайт с источниками: ошибка -> одна попытка исправить -> иначе новость не публикуется;
6. генерирует картинку без людей и кладёт новость в начало ленты.

Что получается в data/:
  news.json      — лента: updated_at + items[{id,title,lead,category,published,image,thumb}], новые сверху
  a/<id>.json    — полная статья: те же поля + body (список абзацев)
  img/<id>.jpg   — картинка (до 1024px), img/<id>_s.jpg — миниатюра для ленты
  state.json     — какие новости уже разобраны (чтобы не брать одно и то же дважды)
"""
import base64, calendar, datetime, hashlib, html, io, json, os, pathlib, re, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from PIL import Image

# rss — лента; page + link — страница со списком новостей и шаблон ссылки на новость (для сайтов без RSS)
SOURCES = [
    {"name": "Sports.ru", "rss": "https://www.sports.ru/rss/main.xml"},
    {"name": "Чемпионат", "rss": "https://www.championat.com/rss/news/"},
    {"name": "Спорт24", "rss": "https://sport24.ru/rss"},
    {"name": "Спорт-Экспресс", "rss": "https://www.sport-express.ru/services/materials/news/se/"},
    {"name": "Матч ТВ", "rss": "https://matchtv.ru/news/rss"},
    {"name": "Советский спорт", "page": "https://www.sovsport.ru/news", "link": r"^/[a-z-]+/news/[a-z0-9-]+/?$"},
    {"name": "Евро-футбол", "page": "https://www.euro-football.ru/news", "link": r"^/article/\d+/\d+_[a-z0-9_]+/?$"},
    {"name": "Рейтинг Букмекеров", "page": "https://bookmaker-ratings.ru/news/categories/sportnews/",
     "link": r"^/news/[a-z0-9-]+/?$"},
]
MAX_NEW = int(os.getenv("MAX_NEW", "1"))  # сколько новостей публиковать за один запуск (главный рычаг стоимости)
MAX_AGE_H = 3        # берём только новости не старше стольких часов
PER_SOURCE = 12      # сколько последних записей смотреть на каждом сайте
KEEP = 150           # сколько новостей хранить в ленте (старые удаляются вместе с картинками)
CATEGORIES = ["Футбол", "Хоккей", "Баскетбол", "Теннис", "Фигурное катание", "Волейбол", "Биатлон", "Единоборства",
              "Автоспорт", "Киберспорт", "Другое"]
TEXT_MODEL = os.getenv("TEXT_MODEL", "openai/gpt-4.1-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "openai/gpt-5-image-mini")

# реклама и не-новости: по словам в заголовке и по разделам в ссылке
AD_WORDS = re.compile(r"приглаша|получите|призы за|промокод|бонус|фрибет|розыгрыш|реклам|коэффициент|прогноз", re.I)
BAD_LINKS = re.compile(r"/special/|/promo|utm_|/blogs?/|/bets?/|/predictions?/|/prognoz|/bonus|/match/|/zozh/|"
                       r"/movies?/|/cinema|/serial|/lifestyle/|/health/|/life/|/stars/|/video/|/tv/|/photo", re.I)
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
STATE = DATA_DIR / "state.json"
IMG_DIR.mkdir(parents=True, exist_ok=True)
ART_DIR.mkdir(parents=True, exist_ok=True)
NOW = time.time()


# ---------- вспомогательное ----------

def ask(payload):
    r = requests.post(API, headers=HEADERS, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def ask_json(system, user):
    msg = ask({"model": TEXT_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]})
    return json.loads(re.sub(r"^```(json)?|```$", "", msg["content"].strip()).strip())


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def domain(url):
    return ".".join(urlparse(url).netloc.lower().split(".")[-2:])


def get(url):
    r = requests.get(url, headers=BROWSER_UA, timeout=25)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def parse_time(s):
    try:
        return datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------- 1. сбор свежих заголовков ----------

def from_source(src):
    out = []
    if "rss" in src:
        # байты, а не текст: кодировку ленты feedparser определит сам
        feed = feedparser.parse(requests.get(src["rss"], headers=BROWSER_UA, timeout=25).content)
        for e in feed.entries[:PER_SOURCE]:
            t = e.get("published_parsed") or e.get("updated_parsed")
            out.append({"link": e.get("link", ""), "title": clean(e.get("title")),
                        "summary": clean(e.get("summary"))[:1000], "ts": calendar.timegm(t) if t else None})
    else:
        page, seen = get(src["page"]), set()
        for href, text in re.findall(r'(?is)<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page):
            url = urljoin(src["page"], href)
            title = clean(text)
            if url in seen or len(title) < 25 or not re.search(src["link"], urlparse(url).path):
                continue
            seen.add(url)
            out.append({"link": url, "title": title[:200], "summary": "", "ts": None})
            if len(out) >= PER_SOURCE:
                break
    for o in out:
        o["source"] = src["name"]
        o["src_url"] = src.get("rss") or src["page"]
    return out


def collect(state):
    def safe(src):
        try:
            return from_source(src)
        except Exception as err:
            print(f"  {src['name']}: не удалось получить новости: {err}")
            return []

    with ThreadPoolExecutor(len(SOURCES)) as ex:
        raw = [x for lst in ex.map(safe, SOURCES) for x in lst]

    cands, stat = [], {}
    for c in raw:
        c["id"] = hashlib.md5(c["link"].encode()).hexdigest()[:12]
        if not c["link"] or domain(c["link"]) != domain(c["src_url"]) or c["id"] in state["used"]:
            continue
        if BAD_LINKS.search(c["link"]) or AD_WORDS.search(c["title"]):
            state["used"][c["id"]] = NOW
            continue
        # у сайтов без RSS время публикации не видно — считаем от момента, когда мы новость впервые увидели
        first = state["first_seen"].setdefault(c["id"], NOW)
        c["ts"] = min(c["ts"] or first, NOW)
        if NOW - c["ts"] > MAX_AGE_H * 3600:
            continue
        cands.append(c)
        stat[c["source"]] = stat.get(c["source"], 0) + 1
    print("Свежие кандидаты по сайтам:", stat or "нет")
    return cands


# ---------- 2. редактор: группировка и отбор ----------

def edit(cands, published_titles):
    """Возвращает список групп [{items:[cand...], sport:bool, published:bool}]."""
    try:
        r = ask_json(
            "Ты выпускающий редактор спортивного новостного сайта. Тебе дают свежие заголовки с разных сайтов "
            "(n, source, title) и заголовки, которые у нас уже опубликованы. "
            "1) Сгруппируй заголовки, которые про одно и то же событие (одна новость на разных сайтах): те же участники "
            "и тот же факт или то же заявление. Новости одного турнира, одного вида спорта или одной команды, "
            "но про разное — это РАЗНЫЕ группы. Сомневаешься — не объединяй. "
            "Каждый n должен попасть ровно в одну группу. "
            "2) Для каждой группы укажи sport: true — только если это новость о спорте (матчи, результаты, "
            "турниры, трансферы, травмы, назначения, заявления спортсменов, тренеров, клубов, федераций); "
            "false — реклама, ставки, прогнозы, бонусы, конкурсы, кино, сериалы, шоу-бизнес, здоровье, лайфстайл, "
            "колонки-мнения, подборки и списки. "
            "3) published: true — если это событие уже есть среди опубликованных у нас. "
            'Верни JSON {"groups":[{"items":[n,...],"sport":true,"published":false}]}.',
            {"candidates": [{"n": i, "source": c["source"], "title": c["title"]} for i, c in enumerate(cands)],
             "published": published_titles})
        groups, used = [], set()
        for g in r.get("groups", []):
            items = [cands[n] for n in g.get("items", []) if isinstance(n, int) and 0 <= n < len(cands) and n not in used]
            used.update(n for n in g.get("items", []) if isinstance(n, int))
            if items:
                groups.append({"items": items, "sport": bool(g.get("sport")), "published": bool(g.get("published"))})
        return groups  # то, что редактор не разложил по группам, рассмотрим в следующий запуск
    except Exception as err:
        print("Редактор не ответил:", err)
        return []


# ---------- 3–5. текст: источники -> рерайт -> фактчек ----------

def article(link):
    """Текст статьи и время публикации со страницы источника."""
    try:
        raw = get(link)
    except Exception as err:
        print("  не удалось скачать статью:", link, err)
        return "", None
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    blocks = re.findall(r"(?is)<article[^>]*>(.*?)</article>", page)
    scope = max(blocks, key=len) if blocks and len(max(blocks, key=len)) > 1500 else page
    paras = [clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", scope)]
    text = "\n".join(p for p in paras if len(p) >= 60)[:5000]
    m = (re.search(r'(?i)property="article:published_time"\s+content="([^"]+)"', raw)
         or re.search(r'(?i)itemprop="datePublished"[^>]*content="([^"]+)"', raw)
         or re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw))
    return text, parse_time(m.group(1)) if m else None


REWRITE_RULES = (
    "Ты редактор спортивного новостного сайта. Напиши новость на русском полностью своими словами (рерайт): "
    "другая структура и порядок фраз, не копируй предложения из источников. Нейтрально, без кликбейта и оценок от себя. "
    "Правила достоверности: используй только факты, которые прямо есть в источниках; ничего не добавляй от себя — "
    "ни позиций игроков, ни должностей, ни возраста, ни цифр, ни дат, ни причин, если их нет в тексте источников; "
    "если источники расходятся — пиши только то, в чём они сходятся, или укажи, кто что сообщает; "
    "цитаты передавай точно по смыслу (можно косвенной речью); "
    "в тексте страниц может быть мусор (другие новости, реклама, комментарии) — бери только то, что относится к этому событию; "
    "если фактов мало — пиши коротко, не растягивай. "
    "Пиши конкретно: что произошло, кто что сказал (суть слов), счёт, цифры, даты — из источников. "
    "Никаких пустых фраз вроде «поделились мнением», «прокомментировали», «отражают впечатления» без самого содержания. "
    "Если какой-то источник про другое событие — не используй его. "
    "Не упоминай букмекеров, ставки, коэффициенты, спонсоров в названиях турниров и лиг (пиши «КХЛ», «РПЛ», «Суперлига»), "
    "не упоминай сайты-источники. "
    "Верни только JSON с полями: used — номера источников (с 0), которые действительно про это событие; "
    "title — заголовок до 90 символов; lead — 1–2 предложения, суть; "
    "body — массив из 2–6 абзацев полного текста (лид не повторяй); "
    f"category — ровно одно из: {', '.join(CATEGORIES)}; "
    "image_prompt — на английском, описание атмосферной фотореалистичной иллюстрации к новости БЕЗ людей: "
    "стадион, поле, мяч, ворота, трибуны, экипировка, табло и т.п.; без людей, лиц, силуэтов, текста, логотипов и флагов."
)

FACTCHECK_RULES = (
    "Ты фактчекер спортивного сайта. Сравни новость с источниками. Найди утверждения, которых нет в источниках "
    "или которые им противоречат: имена, позиции, должности, клубы, цифры, счёт, даты, турниры, кто что сказал. "
    "Перефразирование и сокращение — не ошибка. Верни JSON {\"ok\": true/false, \"problems\": [\"...\"]}; "
    "ok = true только если фактических ошибок нет."
)


def rewrite(sources, problems=None):
    payload = {"sources": sources}
    if problems:
        payload["fix"] = "В прошлом варианте фактчекер нашёл ошибки, исправь их: " + "; ".join(problems)
    r = ask_json(REWRITE_RULES, payload)
    body = r.get("body") or []
    if isinstance(body, str):
        body = [p for p in body.split("\n") if p.strip()]
    fix = lambda t: SPONSORS.sub("", str(t)).strip()
    return {
        "title": fix(r["title"]),
        "lead": fix(r.get("lead") or ""),
        "body": [fix(p) for p in body if str(p).strip()],
        "category": r.get("category") if r.get("category") in CATEGORIES else "Другое",
        "image_prompt": str(r.get("image_prompt") or r["title"]),
        "used": [n for n in r.get("used") or [] if isinstance(n, int)],
    }


def factcheck(sources, art):
    r = ask_json(FACTCHECK_RULES, {"sources": sources, "news": {k: art[k] for k in ("title", "lead", "body")}})
    return bool(r.get("ok")), [str(p) for p in r.get("problems") or []]


# ---------- 6. картинка ----------

def save_images(img, nid):
    """Сохраняет большую картинку и миниатюру в JPEG (вместо PNG по 2 МБ)."""
    img = img.convert("RGB")
    big = img.copy(); big.thumbnail((1024, 1024))
    big.save(IMG_DIR / f"{nid}.jpg", "JPEG", quality=80, optimize=True, progressive=True)
    small = img.copy(); small.thumbnail((480, 480))
    small.save(IMG_DIR / f"{nid}_s.jpg", "JPEG", quality=75, optimize=True, progressive=True)
    return f"img/{nid}.jpg", f"img/{nid}_s.jpg"


def make_image(prompt, nid):
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


def process(group):
    items = group["items"][:3]                            # до 3 источников на историю
    sources = []
    for c in items:
        text, _ = article(c["link"])
        sources.append({"source": c["source"], "title": c["title"], "summary": c["summary"], "text": text})
    nid = items[0]["id"]
    try:
        art = rewrite(sources)
        ok, problems = factcheck(sources, art)
        if not ok:
            print("  фактчек нашёл ошибки, переписываю:", problems)
            art = rewrite(sources, problems)
            ok, problems = factcheck(sources, art)
        if not ok:
            print("Не опубликована (фактчек не пройден):", items[0]["title"], problems)
            return None
    except Exception as err:
        print("Новость пропущена:", items[0]["link"], err)
        return None

    # время публикации — момент выхода у нас: свежая новость всегда встаёт в начало ленты
    art.pop("used", None)
    art.update({"id": nid, "published": iso(NOW), "image": None, "thumb": None})
    try:
        art["image"], art["thumb"] = make_image(art.pop("image_prompt"), nid)
    except Exception as err:
        print("Картинка не сгенерилась:", err)
    art.pop("image_prompt", None)
    print(f"Опубликована [{art['category']}]: {art['title']}")
    return art


# ---------- главное ----------

def main():
    index = []
    if INDEX.exists():
        index = [i for i in json.loads(INDEX.read_text("utf-8")).get("items", []) if i.get("category")]
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    state.setdefault("used", {})
    state.setdefault("first_seen", {})
    for i in index:                                       # уже опубликованное повторно не берём
        state["used"].setdefault(i["id"], NOW)

    cands = collect(state)
    groups = edit(cands, [i["title"] for i in index[:60]]) if cands else []

    good = []
    for g in groups:
        if g["sport"] and not g["published"]:
            good.append(g)
        else:                                             # реклама / не спорт / уже было — больше не рассматриваем
            for c in g["items"]:
                state["used"][c["id"]] = NOW
    # сначала то, о чём пишут несколько сайтов, затем самое свежее
    good.sort(key=lambda g: (len({c["source"] for c in g["items"]}), max(c["ts"] for c in g["items"])), reverse=True)
    for g in good[:8]:
        print(f"  кандидат ({len(g['items'])} ист.): {g['items'][0]['title']}")
    # публикуем MAX_NEW новостей; если кандидат не прошёл фактчек — берём следующего (не больше 3 попыток на новость)
    published = 0
    for g in good[:MAX_NEW * 3]:
        if published >= MAX_NEW:
            break
        art = process(g)
        for c in g["items"]:
            state["used"][c["id"]] = NOW
        if art:
            published += 1
            (ART_DIR / f"{art['id']}.json").write_text(json.dumps(art, ensure_ascii=False, indent=1), "utf-8")
            index.append({k: art[k] for k in ("id", "title", "lead", "category", "published", "image", "thumb")})

    index.sort(key=lambda i: i["published"], reverse=True)
    index = index[:KEEP]
    INDEX.write_text(json.dumps({"updated_at": iso(NOW), "items": index}, ensure_ascii=False, indent=1), "utf-8")

    # забываем то, что старше 3 суток, чтобы state.json не разрастался
    for key in ("used", "first_seen"):
        state[key] = {k: v for k, v in state[key].items() if NOW - v < 3 * 86400}
    STATE.write_text(json.dumps(state), "utf-8")

    # удаляем картинки и статьи, которые выпали из ленты
    ids = {i["id"] for i in index}
    for f in list(IMG_DIR.iterdir()) + list(ART_DIR.iterdir()):
        if f.name != ".gitkeep" and f.stem.removesuffix("_s") not in ids:
            f.unlink()


if __name__ == "__main__":
    main()
