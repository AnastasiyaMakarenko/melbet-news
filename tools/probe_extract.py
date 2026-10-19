"""Разово: сколько текста генератор достаёт со страниц источников (без нейронки)."""
import re, sys
sys.path.insert(0, ".")
import generate as g
for src in g.SOURCES:
    try:
        items = g.from_source(src)[:4]
    except Exception as e:
        print(src["name"], "ERR", e); continue
    for c in items:
        try:
            raw = g.get(c["link"])
        except Exception as e:
            print(f"  [{src['name']}] ERR {e.__class__.__name__} {c['link'][:80]}"); continue
        ours, _ = g.article(c["link"])
        page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
        allp = [g.clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
        allp = [p for p in allp if len(p) >= 40]
        print(f"  [{src['name']}] наш текст: {len(ours.split())} слов | все <p> на странице: {len(' '.join(allp).split())} слов | {c['title'][:60]}")
