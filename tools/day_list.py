"""БЕСПЛАТНО (без нейронки): список заметок архива «Советского спорта» за нужные дни и виды спорта —
чтобы вручную выбрать события, о которых вышло несколько заметок. Вход: DAYS = [["2026-10-17", "hockey"], ...]."""
import collections, json, os, sys
sys.path.insert(0, ".")
import generate as g
from backfill import from_sitemap, day_of
import datetime
items = from_sitemap()
for day, sport in json.loads(os.environ["DAYS"]):
    d = datetime.date.fromisoformat(day)
    rows = [c for c in items if day_of(c["ts"]) == d and f"/{sport}/" in c["link"] and not g.BAD_LINKS.search(c["link"])]
    print(f"\n===== {day} {sport}: {len(rows)} заметок")
    for c in sorted(rows, key=lambda c: c["link"]):
        print("  ", c["link"].rsplit("/", 1)[-1][:110])
