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
  archive.json   — все живые статьи за ARCHIVE_DAYS дней (лента короче: в ней только последние KEEP)
  sitemap.xml    — карта сайта для поисковиков, пересобирается при каждом запуске

id статьи = её ЧПУ-адрес (транслит заголовка), например sbornaya-rossii-obygrala-iran.
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
ARCHIVE_DAYS = 3650  # сколько дней статья и её картинки остаются доступны по своему адресу (10 лет = практически не удаляем)
# Адреса на сайте — должны совпадать с настройками CONFIG в news.txt. Нужны только для sitemap.xml.
# ЧПУ без параметров: /ru/news — все новости, /ru/news/football — раздел, /ru/news/<адрес> — новость
SITE_LIST_URL = os.getenv("SITE_LIST_URL", "https://melbet.ru/ru/news")
SITE_NEWS_URL = os.getenv("SITE_NEWS_URL", SITE_LIST_URL + "/{slug}")
SITE_CAT_URL = os.getenv("SITE_CAT_URL", SITE_LIST_URL + "/{slug}")
CAT_SLUGS = {"Футбол": "football", "Хоккей": "hockey", "Баскетбол": "basketball", "Теннис": "tennis",
             "Фигурное катание": "figure-skating", "Волейбол": "volleyball", "Биатлон": "biathlon",
             "Единоборства": "martial-arts", "Автоспорт": "motorsport", "Киберспорт": "esports", "Другое": "other"}
CATEGORIES = ["Футбол", "Хоккей", "Баскетбол", "Теннис", "Фигурное катание", "Волейбол", "Биатлон", "Единоборства",
              "Автоспорт", "Киберспорт", "Другое"]
TEXT_MODEL = os.getenv("TEXT_MODEL", "openai/gpt-4.1-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "google/gemini-nano-banana-2.1")

# реклама и не-новости: по словам в заголовке и по разделам в ссылке
AD_WORDS = re.compile(r"приглаша|получите|призы за|промокод|бонус|фрибет|розыгрыш|реклам|коэффициент|прогноз", re.I)
BAD_LINKS = re.compile(r"/special/|/promo|utm_|/blogs?/|/bets?/|/predictions?/|/prognoz|/bonus|/match/|/zozh/|"
                       r"/movies?/|/cinema|/serial|/lifestyle/|/health/|/life/|/stars/|/video/|/tv/|/photo", re.I)
# спонсоры-букмекеры в названиях турниров («Фонбет КХЛ» -> «КХЛ»): на сайте Мелбет конкурентов не упоминаем
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
ARCHIVE = DATA_DIR / "archive.json"
SITEMAP = DATA_DIR / "sitemap.xml"
TAKEN = set()        # занятые адреса статей (заполняется в main)
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


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
                     "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(title):
    """ЧПУ из заголовка: «Сборная России обыграла Иран» -> sbornaya-rossii-obygrala-iran (уникально)."""
    text = "".join(TRANSLIT.get(ch, ch) for ch in title.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    slug = ""
    for w in words:
        if slug and len(slug) + len(w) + 1 > 70:
            break
        slug = f"{slug}-{w}" if slug else w
    slug = slug[:70] or "news"
    base, n = slug, 2
    while slug in TAKEN:
        slug, n = f"{base}-{n}", n + 1
    TAKEN.add(slug)
    return slug


def write_sitemap(archive):
    def url(loc, lastmod):
        return f"  <url><loc>{html.escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>"

    archive = sorted(archive, key=lambda a: a["published"], reverse=True)
    newest = archive[0]["published"] if archive else iso(NOW)
    rows = [url(SITE_LIST_URL, newest)]
    for cat in CATEGORIES:
        dates = [a["published"] for a in archive if a["category"] == cat]
        if dates:
            rows.append(url(SITE_CAT_URL.format(slug=CAT_SLUGS[cat]), dates[0]))
    rows += [url(SITE_NEWS_URL.format(slug=a["id"]), a["published"]) for a in archive]
    SITEMAP.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                       '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                       + "\n".join(rows) + "\n</urlset>\n", "utf-8")


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
            "false — реклама, ставки, коэффициенты, прогнозы, букмекеры и их спонсорство, бонусы, конкурсы, кино, "
            "сериалы, шоу-бизнес, здоровье, лайфстайл, колонки-мнения, подборки и списки, промежуточные онлайн-обновления "
            "идущих матчей. "
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


# правила для картинок: живые, разные, по сути конкретной новости — но без людей (требование проекта)
IMAGE_RULES = (
    "describe ONE bright, vivid scene that looks like a real photo by a professional sports photojournalist for THIS story "
    "(full-frame camera, 24-35mm or 85mm lens, real light, slight natural imperfections and film grain). Colours rich and "
    "saturated, strong dramatic light: floodlights at night, golden hour sun, blue hour sky, coloured stadium seats, "
    "team colours of the clubs in the story (on seats, scarves on railings, kits hanging, nets, goal frames, smoke from "
    "flares). Show the real sports venue and equipment with a detail that fits the story. AVOID the boring and the "
    "artificial look: no lone ball centred on grass, no symmetrical empty goal shot, no CGI, 3D render, plastic or "
    "over-smooth surfaces, no fake glow. "
    "NEVER name real stadiums, arenas, cities, clubs, national teams or countries in the description — describe a generic "
    "modern venue and use only colours (e.g. 'seats in red and white'), so that no real lettering, crests or flags appear. "
    "The venue is EMPTY: no game in progress, empty seats and stands (before or after the match), never describe a packed "
    "or full arena, spectators, fans, teams, players or referees. "
    "STRICTLY no people and no body parts: no faces, hands, silhouettes, crowds or players; no text, letters, numbers, logos or flags; "
    "no advertising boards, betting slips, odds screens, casino chips, cards or anything related to gambling; "
    "no club crests, emblems, stars or mascots on seats, scarves or flags; no captain armbands; no staff or stewards even far away."
)

# набор сюжетов: каждой новости достаётся свой (по её адресу) — чтобы картинки не повторяли одно и то же
SCENES = [
    "aerial drone shot of the whole stadium at night with floodlights on and the city lights around",
    "wide shot from the top row of the stands at golden hour, long shadows across the pitch or ice",
    "the players' tunnel leading out to a brightly lit arena, view from inside the tunnel",
    "the substitutes' bench and dugout with team jackets and water bottles, the arena behind",
    "the dressing room with kits in team colours hanging in a row, boots below, warm light",
    "rain-soaked pitch under floodlights, puddles reflecting the lights, drops in the air",
    "light snow falling over the stadium at night, lit by floodlights",
    "coloured smoke from flares drifting over empty stands in team colours",
    "fireworks and confetti over the arena after a match",
    "low angle from ground level at the corner flag or the goal line, stadium towering above",
    "the trophy or medals on a pedestal in a dark arena under a spotlight",
    "the stadium exterior at blue hour with the facade lit in team colours",
    "close view of the goal net bulging with the ball inside, stadium lights bokeh behind",
    "the scoreboard and video screens glowing over an empty arena (screens without any text)",
    "the ice rink from above with the fresh ice shining, the boards and nets in team colours",
    "the court or hall from the top tier with dramatic spotlights cutting through haze",
    "training ground in the morning: cones, mannequins, balls in a net bag, mist over the grass",
    "press conference room with microphones on the table and a backdrop in team colours (no text)",
]


def scene_for(nid):
    return SCENES[int(hashlib.md5(nid.encode()).hexdigest(), 16) % len(SCENES)]

# ЮРИДИЧЕСКОЕ ТРЕБОВАНИЕ (юротдел Мелбет, 19.10.2026): новости о спорте, в т.ч. российские и о российских спортсменах,
# можно — но БЕЗ любых упоминаний ставок и всего, что связано с букмекерами: акции, фрибеты, бонусы, коэффициенты,
# прогнозы, названия БК (в т.ч. в спонсорских названиях турниров, лиг, клубов, арен, команд). Ни явно, ни скрыто.
LEGAL_RULES = (
    "ЮРИДИЧЕСКОЕ ТРЕБОВАНИЕ: в тексте не должно быть НИКАКИХ упоминаний ставок и всего, что связано с букмекерами — "
    "ни явно, ни косвенно, ни скрыто: ставки, пари, коэффициенты, котировки, прогнозы на исход, фавориты/андердоги "
    "по мнению букмекеров, тотализатор, беттинг, азартные игры, казино; акции, бонусы, фрибеты, промокоды, розыгрыши, "
    "конкурсы прогнозов; названия любых букмекерских компаний и их спонсорство (Фонбет, Winline, Лига Ставок, "
    "Бетсити, BetBoom, Олимпбет, Марафонбет, PARI, Тенниси, Балтбет, 1xBet, Мелбет и любые другие) — в том числе "
    "внутри спонсорских названий турниров, лиг, кубков, арен, клубов и команд: пиши без спонсора («КХЛ», «РПЛ», "
    "«Кубок России», «Суперлига»), клуб «Пари НН» называй «Нижний Новгород», киберкоманды со спонсором-букмекером "
    "в названии не упоминай. Если новость без этого теряет смысл (она про ставки, букмекера, спонсорство БК) — "
    "её нельзя публиковать."
)

REWRITE_RULES = (
    "Ты редактор спортивного новостного сайта. Напиши новость на русском полностью своими словами (рерайт): "
    "другая структура и порядок фраз, не копируй предложения из источников. Нейтрально, без кликбейта и оценок от себя. "
    "Правила достоверности: используй только факты, которые прямо есть в источниках; ничего не добавляй от себя — "
    "ни позиций игроков, ни должностей, ни возраста, ни цифр, ни дат, ни причин, если их нет в тексте источников; "
    "ОСОБЕННО: не дописывай имена и отчества к фамилиям (если в источнике «Новиков» — пиши «Новиков», а не «Александр "
    "Новиков»), не придумывай, дома или в гостях прошёл матч, номер тура, стадион, город — только если это есть в источнике; "
    "если источники расходятся — пиши только то, в чём они сходятся, или укажи, кто что сообщает; "
    "прямую речь (в кавычках или абзацем с тире) приводи ТОЛЬКО дословно из источника, ничего в ней не меняя; "
    "пересказ чужих слов — косвенной речью; "
    "в тексте страниц может быть мусор (другие новости, реклама, комментарии) — бери только то, что относится к этому событию; "
    "СТИЛЬ — как новости Матч ТВ, Sports.ru, «Советского спорта»: плотный подробный текст для поисковой выдачи. "
    "Порядок: (1) лид — кто, что сделал или заявил, где, когда, в каком турнире и туре; "
    "(2) контекст — дата, счёт (по таймам/периодам, авторы голов и минуты, если есть), положение в таблице с очками; "
    "(3) прямая речь участников — дословные цитаты из источника отдельными абзацами в формате «— Текст цитаты, — сказал "
    "Фамилия.» (длинную цитату разбей на 2–3 абзаца; интервью — вопрос отдельным абзацем, ответ абзацем с тире); "
    "(4) предыстория и статистика из источника — прошлые клубы, рекорды, цифры сезона, возраст; "
    "(5) что дальше — следующий матч или турнир с датой, если есть в источниках. "
    "Используй ВСЕ относящиеся к событию факты из всех источников. Главные имена, клубы и турнир повторяй естественно "
    "3–5 раз, чередуя формы: полное имя → фамилия → описание («40-летний защитник», «бывший полузащитник «Спартака»»); "
    "клуб → прозвище или описание («красно-белые», «московская команда», «команда Карседо», «казанцы»). Без переспама. "
    "Конкретика: возраст, очки, место, статистика, даты, минуты — только из источников. "
    "Ссылки на СМИ и агентства (ТАСС, РИА, «Матч ТВ», Sports.ru и т.п.) не упоминай — пиши «заявил в интервью», "
    "«сообщил журналистам», «по данным СМИ». Хоккейную статистику «коэффициент полезности» называй «показатель полезности». "
    "Если фактов мало — не выдумывай и не лей воду, пиши короче. "
    "Названия клубов, турниров и имена — по-русски, как в российских СМИ (клубы в кавычках-ёлочках: «Манчестер Сити»). "
    "Пиши конкретно: что произошло, кто что сказал (суть слов), счёт, цифры, даты — из источников. "
    "Никаких пустых фраз вроде «поделились мнением», «прокомментировали», «отражают впечатления» без самого содержания. "
    "Если какой-то источник про другое событие — не используй его. "
    + LEGAL_RULES + " "
    "Не упоминай сайты-источники и не пиши служебных фраз о самих источниках («в источнике нет подробностей», "
    "«в доступных материалах не раскрывается», «нельзя подтвердить»). "
    "Пиши только о завершившихся событиях; промежуточный счёт идущего матча — не новость. "
    "Если в источниках нет конкретной новости (пустой текст, только заголовок, недостаточно фактов) или новость "
    "нарушает юридическое требование — верни только {\"skip\": true}, НЕ пиши текст о том, что данных нет. "
    "Верни только JSON с полями: used — номера источников (с 0), которые действительно про это событие; "
    "title — заголовок 60–140 символов с главными именами и клубами; если есть яркое заявление — в формате "
    "«Фамилия — о чём: «дословная цитата»» или «Кто Фамилия: суть заявления»; "
    "lead — 1–2 предложения: кто, что, где, когда, итог; "
    "body — массив из 4–8 абзацев полного текста, 250–450 слов, если позволяют факты (лид не повторяй); "
    f"category — ровно одно из: {', '.join(CATEGORIES)}; "
    "image_prompt — на английском: " + IMAGE_RULES
)

FACTCHECK_RULES = (
    "Ты фактчекер спортивного сайта. Сравни новость с источниками. Найди утверждения, которых нет в источниках "
    "или которые им противоречат: имена, позиции, должности, клубы, цифры, счёт, даты, турниры, кто что сказал. "
    "Прямая речь (в кавычках или абзацем с тире) должна совпадать с источником дословно — иначе это ошибка. "
    "Отдельно проверь КАЖДОЕ имя и отчество (если в источнике только фамилия, а в новости добавлено имя — это ошибка), "
    "а также «дома/в гостях», номер тура, стадион и город. "
    "Перефразирование и сокращение остального текста — не ошибка. Также проверь юридическое требование: " + LEGAL_RULES + " "
    "Любое его нарушение — тоже ошибка. Верни JSON {\"ok\": true/false, \"problems\": [\"...\"]}; "
    "ok = true только если нет ни фактических ошибок, ни нарушений юридического требования."
)


# признаки «новости», которую нейронка написала вместо отказа, или служебных фраз про источники
NO_DATA = re.compile(r"нет данных|недостаточно (данных|информации|фактов)|не удалось|информация отсутствует|"
                     r"в источник\w* (не |нет|отсутству)|не указан\w* в источник|в источник\w* не указ|доступных материал|нельзя подтвердить|не раскрыва", re.I)
# ставки и всё, что связано с букмекерами (юридическое требование). «Пари Сен-Жермен» — название ПСЖ, не БК
BETTING = re.compile(r"\bставк|\bставок\b|ставочн|коэффициент|котировк|букмекер|\bБК\b|фрибет|бонус|промокод|"
                     r"прогноз|розыгрыш|тотализатор|беттинг|betting|казино|азарт|\bпари\b(?!\s+сен)|\bPARI\b|"
                     r"Фонбет|Fonbet|Winline|Винлайн|Лиг\w* Ставок|Бетсити|BetCity|BetBoom|Бетбум|Олимпбет|Olimpbet|"
                     r"Марафонбет|Marathonbet|\bТенниси\b|\bTennisi\b|Балтбет|Baltbet|1xBet|1хСтавк|Мелбет|Melbet|Pinnacle|bet365",
                     re.I)


# слово, в котором перемешаны русские и латинские буквы («Галaktionов») — опечатка нейронки
MIXED = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w+\b", re.I)


def violations(art):
    """Что в готовой новости нарушает требования: ставки/БК или служебные фразы."""
    text = " ".join([art["title"], art["lead"]] + art["body"])
    found = sorted({m.group(0) for m in BETTING.finditer(text)} | {m.group(0) for m in NO_DATA.finditer(text)})
    found += sorted({m.group(0) for m in MIXED.finditer(text)})
    return [f"убери из текста: «{w}» — запрещено (ставки/букмекеры, служебная фраза или слово из смеси русских и латинских букв)"
            for w in found]


def rewrite(sources, problems=None):
    payload = {"sources": sources}
    if problems:
        payload["fix"] = "В прошлом варианте фактчекер нашёл ошибки, исправь их: " + "; ".join(problems)
    r = ask_json(REWRITE_RULES, payload)
    if r.get("skip") or not r.get("title") or NO_DATA.search(str(r.get("title"))):
        raise ValueError("в источниках недостаточно фактов для новости")
    body = r.get("body") or []
    if isinstance(body, str):
        body = [p for p in body.split("\n") if p.strip()]
    # бренд пишем только кириллицей — юридическое требование
    fix = lambda t: re.sub(r"\bmelbet\b(?![.\-/]\w)", "Мелбет", SPONSORS.sub("", str(t)), flags=re.I).strip()
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
    prompt = prompt + " Scene idea for variety (adapt it to the story and this sport): " + scene_for(nid) + "."
    last = None
    for attempt in range(3):                              # модель иногда отвечает без картинки — пробуем ещё
        try:
            msg = ask({
                "model": IMAGE_MODEL,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content":
                    "Generate a landscape 3:2 photo: bright, vivid, real sports photojournalism, not CGI. " + prompt +
                    " Absolutely no people or body parts, no faces, hands, silhouettes or crowds, no text, numbers or logos."}],
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
    try:
        art = rewrite(sources)
        ok, problems = factcheck(sources, art)
        problems = problems + violations(art)
        if not ok or problems:
            print("  фактчек нашёл ошибки, переписываю:", problems)
            art = rewrite(sources, problems)
            ok, problems = factcheck(sources, art)
        if not ok:
            print("Не опубликована (фактчек не пройден):", items[0]["title"], problems)
            return None
        if violations(art):                               # юридическое требование — без исключений
            print("Не опубликована (ставки/БК или служебный текст):", items[0]["title"], violations(art))
            return None
        if len(art["body"]) < 2:                          # новость без нормального текста не публикуем
            print("Не опубликована (слишком мало текста):", items[0]["title"])
            return None
    except Exception as err:
        print("Новость пропущена:", items[0]["link"], err)
        return None

    # время публикации — момент выхода у нас: свежая новость всегда встаёт в начало ленты
    art.pop("used", None)
    nid = slugify(art["title"])                           # адрес статьи на сайте
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
    # архив: всё, что доступно по своему адресу (лента — только его свежая часть)
    archive = json.loads(ARCHIVE.read_text("utf-8")) if ARCHIVE.exists() else \
        [{k: i[k] for k in ("id", "title", "category", "published")} for i in index]
    TAKEN.update(a["id"] for a in archive)
    TAKEN.update(CAT_SLUGS.values())                      # адрес новости не может совпасть с адресом раздела

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
            archive.append({k: art[k] for k in ("id", "title", "category", "published")})

    index.sort(key=lambda i: i["published"], reverse=True)
    index = index[:KEEP]
    INDEX.write_text(json.dumps({"updated_at": iso(NOW), "items": index}, ensure_ascii=False, indent=1), "utf-8")

    # забываем то, что старше 3 суток, чтобы state.json не разрастался
    for key in ("used", "first_seen"):
        state[key] = {k: v for k, v in state[key].items() if NOW - v < 3 * 86400}
    STATE.write_text(json.dumps(state), "utf-8")

    # статьи старше ARCHIVE_DAYS уходят из архива; sitemap пересобираем
    in_feed = {i["id"] for i in index}
    limit = iso(NOW - ARCHIVE_DAYS * 86400)
    archive = [a for a in archive if a["published"] >= limit or a["id"] in in_feed]
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=1), "utf-8")
    write_sitemap(archive)

    # удаляем картинки и статьи, которых больше нет в архиве
    ids = {a["id"] for a in archive} | in_feed
    for f in list(IMG_DIR.iterdir()) + list(ART_DIR.iterdir()):
        # img/<id>.jpg, img/<id>_s.jpg, img/<id>-v2.jpg (перегенерированная картинка) — всё это файлы статьи <id>
        if f.name != ".gitkeep" and re.sub(r"(-v\d+)?(_s)?$", "", f.stem) not in ids:
            f.unlink()


if __name__ == "__main__":
    main()
