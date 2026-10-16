"""Разово: добавить в ленту новости за прошедшие дни — по PER_DAY самых значимых событий на день,
с настоящей датой публикации (время из источника). Тот же конвейер: рерайт, фактчек, картинка без людей.

Запуск:  python tools/backfill.py dry   — только показать, за какие дни есть новости (без нейронки, бесплатно)
         python tools/backfill.py       — опубликовать
"""
import collections, datetime, json, pathlib, sys

sys.path.insert(0, ".")
import generate as g

DAYS = 7          # сколько прошедших дней заполнить (без сегодняшнего)
PER_DAY = 2       # сколько новостей на день
MSK = datetime.timezone(datetime.timedelta(hours=3))
DRY = len(sys.argv) > 1 and sys.argv[1] == "dry"


def day_of(ts):
    return datetime.datetime.fromtimestamp(ts, MSK).date()


# архив по дням: у RSS только последние 2 дня, а в карте сайта «Советского спорта» — около месяца новостей с датами
SITEMAP = ("Советский спорт", "https://www.sovsport.ru/sitemap-news.xml", "https://www.sovsport.ru/news")


def from_sitemap():
    name, url, src_url = SITEMAP
    try:
        xml = g.get(url)
    except Exception as err:
        print(f"  {name} (архив): не удалось: {err}")
        return []
    out = []
    for loc, mod in g.re.findall(r"<url>\s*<loc>\s*([^<]+?)\s*</loc>\s*<lastmod>\s*([^<]+?)\s*</lastmod>", xml):
        ts = g.parse_time(mod)
        if not ts or "/news/" not in loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        # заголовка в карте нет — даём редактору адрес новости (транслит), полный текст возьмём со страницы
        out.append({"link": loc, "title": slug.replace("-", " "), "summary": "", "ts": ts,
                    "source": name, "src_url": src_url})
    if out:
        print(f"  {name} (архив): {len(out)} записей, с {day_of(min(c['ts'] for c in out))} по {day_of(max(c['ts'] for c in out))}")
    return out


def candidates(state, published_titles):
    g.PER_SOURCE = 1000                                   # берём всю ленту источника, а не только свежие записи
    raw = []
    for src in g.SOURCES:
        if "rss" not in src:                              # у сайтов без RSS не видно даты — для прошлых дней не годятся
            continue
        try:
            got = g.from_source(src)
        except Exception as err:
            print(f"  {src['name']}: не удалось: {err}")
            continue
        dated = [c for c in got if c.get("ts")]
        if dated:
            print(f"  {src['name']}: {len(dated)} записей, с {day_of(min(c['ts'] for c in dated))} по {day_of(max(c['ts'] for c in dated))}")
        raw += dated
    raw += from_sitemap()
    today = day_of(g.NOW)
    days = [today - datetime.timedelta(days=i) for i in range(1, DAYS + 1)]
    by_day = collections.defaultdict(list)
    for c in raw:
        c["id"] = g.hashlib.md5(c["link"].encode()).hexdigest()[:12]
        if c["id"] in state["used"] or g.BAD_LINKS.search(c["link"]) or g.AD_WORDS.search(c["title"]):
            continue
        if g.domain(c["link"]) != g.domain(c["src_url"]):
            continue
        d = day_of(c["ts"])
        if d in days:
            by_day[d].append(c)
    return days, by_day


def pick(cands, published_titles):
    """Редактор выбирает самые значимые события дня (с группировкой одного события с разных сайтов)."""
    cands = sorted(cands, key=lambda c: c["ts"], reverse=True)[:120]
    r = g.ask_json(
        "Ты выпускающий редактор спортивного сайта. Перед тобой заголовки новостей ОДНОГО дня с разных сайтов. "
        "Выбери до 5 самых значимых и интересных широкой аудитории СПОРТИВНЫХ событий этого дня (результаты матчей, "
        "турниры, трансферы, назначения, травмы, громкие заявления). По возможности — разные виды спорта. "
        "Не бери промежуточные онлайн-обновления (счёт после тайма, стартовые составы, трансляции, «идёт матч») — "
        "только завершённые события. Не бери рекламу, ставки, прогнозы, кино, лайфстайл, колонки-мнения, подборки, а также то, что уже есть среди "
        "опубликованных. Некоторые заголовки даны транслитом из адреса страницы — читай их как русский текст. "
        "Для каждого события перечисли номера ВСЕХ заголовков про это же событие. "
        'Верни JSON {"picks":[{"items":[n,...]}]} — от самого важного к менее важному.',
        {"candidates": [{"n": i, "source": c["source"], "title": c["title"]} for i, c in enumerate(cands)],
         "published": published_titles})
    groups = []
    for p in (r if isinstance(r, list) else r.get("picks", [])):
        items = [cands[n] for n in p.get("items", []) if isinstance(n, int) and 0 <= n < len(cands)]
        if items:
            groups.append({"items": items})
    return groups


def main():
    d = g.DATA_DIR
    index = json.loads(g.INDEX.read_text("utf-8")).get("items", [])
    archive = json.loads(g.ARCHIVE.read_text("utf-8"))
    state = json.loads(g.STATE.read_text("utf-8")) if g.STATE.exists() else {"used": {}, "first_seen": {}}
    state.setdefault("used", {}); state.setdefault("first_seen", {})
    g.TAKEN.update(a["id"] for a in archive)
    g.TAKEN.update(g.CAT_SLUGS.values())
    titles = [i["title"] for i in index]

    days, by_day = candidates(state, titles)
    for day in days:
        print(f"{day}: {len(by_day[day])} кандидатов")
    if DRY:
        return

    added = 0
    for day in days:
        if not by_day[day]:
            continue
        try:
            groups = pick(by_day[day], titles)
        except Exception as err:
            print(f"{day}: редактор не ответил: {err}")
            continue
        done = sum(1 for i in index if day_of(g.parse_time(i["published"])) == day)   # уже есть за этот день
        for grp in groups:
            if done >= PER_DAY:
                break
            # точное время публикации — со страницы новости (в карте сайта может стоять время правки)
            times = []
            for c in grp["items"][:3]:
                _, ts = g.article(c["link"])
                times.append(ts or c["ts"])
            art = g.process(grp)
            for c in grp["items"]:
                state["used"][c["id"]] = g.NOW
            if not art:
                continue
            art["published"] = g.iso(min(times))           # настоящее время публикации события
            (g.ART_DIR / f"{art['id']}.json").write_text(json.dumps(art, ensure_ascii=False, indent=1), "utf-8")
            index.append({k: art[k] for k in ("id", "title", "lead", "category", "published", "image", "thumb")})
            archive.append({k: art[k] for k in ("id", "title", "category", "published")})
            titles.append(art["title"])
            done += 1
            added += 1
            print(f"{day}: опубликована «{art['title']}» ({art['published']})")

    index.sort(key=lambda i: i["published"], reverse=True)
    g.INDEX.write_text(json.dumps({"updated_at": g.iso(g.NOW), "items": index[:g.KEEP]}, ensure_ascii=False, indent=1), "utf-8")
    g.ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=1), "utf-8")
    g.STATE.write_text(json.dumps(state), "utf-8")
    g.write_sitemap(archive)
    print(f"Добавлено новостей: {added}")


if __name__ == "__main__":
    main()
