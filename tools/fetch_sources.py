"""Разово и БЕСПЛАТНО (без нейронки): найти и скачать исходники для уже опубликованных новостей, чтобы переписать их вручную.
Поиск — по ключевым словам в адресах статей (архив «Советского спорта» + свежие RSS), ±2 дня от даты новости.
Ключевые слова передаются при запуске (KEYS: {id: [[вариант, ...], [вариант, ...]], ...} — все группы должны совпасть).
Результат — sources_dump.json (выгружается как временный файл запуска, в репозиторий не попадает)."""
import json, os, sys

sys.path.insert(0, ".")
import generate as g
from backfill import from_sitemap

# режим 2: прямые ссылки {ключ: [url, ...]} — скачать именно их
if os.environ.get("LINKS"):
    out = {}
    for key, urls in json.loads(os.environ["LINKS"]).items():
        got = []
        for u in urls:
            text, ts = g.article(u)
            got.append({"link": u, "text": text, "ts": ts})
            print(f"{key}: {g.words(text)} слов | {u.rsplit('/', 1)[-1][:70]}")
        out[key] = got
    g.pathlib.Path("sources_dump.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    sys.exit(0)

keys = json.loads(os.environ["KEYS"])
index = {i["id"]: i for i in json.loads(g.INDEX.read_text("utf-8"))["items"]}

pool = from_sitemap()
for src in g.SOURCES:
    if "rss" in src:
        try:
            g.PER_SOURCE = 1000
            pool += [c for c in g.from_source(src) if c.get("ts")]
        except Exception as err:
            print("  нет ленты:", src["name"], err)

out = {}
for nid, groups in keys.items():
    item = index.get(nid)
    if not item:
        print("нет такой новости:", nid); continue
    t0 = g.parse_time(item["published"])
    hits = []
    for c in pool:
        link = c["link"].lower()
        if abs(c["ts"] - t0) > 2 * 86400 or g.BAD_LINKS.search(link):
            continue
        if all(any(v in link for v in grp) for grp in groups) and c["link"] not in [h["link"] for h in hits]:
            hits.append(c)
    got = []
    for c in hits[:4]:
        text, _ = g.article(c["link"])
        if g.words(text) >= 40:
            got.append({"source": c["source"], "link": c["link"], "title": c["title"], "text": text})
    out[nid] = {"our_title": item["title"], "published": item["published"], "sources": got}
    print(f"{item['title'][:70]} | найдено {len(hits)}, с текстом {len(got)}: {[g.words(x['text']) for x in got]}")

pathlib = g.pathlib
pathlib.Path("sources_dump.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
