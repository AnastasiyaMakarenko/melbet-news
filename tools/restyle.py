"""Разово: переписать уже опубликованные новости в новом стиле (как крупные спортивные СМИ, с учётом юр. требований).
Адрес (id), дата и картинка новости НЕ меняются — меняются заголовок, лид и текст.
Исходник ищется заново в архиве «Советского спорта» за те же сутки; если не найден — текст перестраивается
из уже опубликованного (без новых фактов). Если новый вариант не проходит фактчек/проверки — остаётся старый."""
import json, re, sys

sys.path.insert(0, ".")
import generate as g
from backfill import from_sitemap  # архив «Советского спорта» с датами

DAY = 36 * 3600


def find_sources(art, archive_items):
    near = [c for c in archive_items if abs(c["ts"] - g.parse_time(art["published"])) < DAY]
    if not near:
        return []
    near = near[:300]
    r = g.ask_json(
        "Найди среди заголовков (транслит из адреса страницы — читай как русский текст) новости про ТО ЖЕ САМОЕ событие, "
        "что и наша новость: те же участники и тот же факт. Верни JSON {\"items\": [n, ...]} — до 3 номеров, или пустой "
        "список, если такой новости нет. Не угадывай: только точное совпадение события.",
        {"our": {"title": art["title"], "lead": art["lead"]},
         "candidates": [{"n": i, "title": c["title"]} for i, c in enumerate(near)]})
    items = r if isinstance(r, list) else r.get("items", [])
    return [near[n] for n in items if isinstance(n, int) and 0 <= n < len(near)][:3]


def main():
    d = g.DATA_DIR
    index = json.loads(g.INDEX.read_text("utf-8"))
    archive = json.loads(g.ARCHIVE.read_text("utf-8"))
    sitemap_items = from_sitemap()
    changed = 0
    for item in index["items"]:
        path = g.ART_DIR / f"{item['id']}.json"
        old = json.loads(path.read_text("utf-8"))
        found = find_sources(old, sitemap_items)
        sources = []
        for c in found:
            text, _ = g.article(c["link"])
            if text:
                sources.append({"source": c["source"], "title": c["title"], "summary": "", "text": text})
        origin = "исходник найден" if sources else "по опубликованному тексту"
        if not sources:                                    # перестраиваем то, что уже опубликовано, без новых фактов
            sources = [{"source": "наш текст", "title": old["title"], "summary": old["lead"], "text": "\n".join(old["body"])}]
        try:
            new = g.rewrite(sources)
            ok, problems = g.factcheck(sources, new)
            problems += g.violations(new)
            if not ok or problems:
                new = g.rewrite(sources, problems)
                ok, problems = g.factcheck(sources, new)
                problems += g.violations(new)
            if not ok or problems or len(new["body"]) < 2:
                print(f"ОСТАВЛЕН СТАРЫЙ ({origin}): {old['title']} | {problems}")
                continue
        except Exception as err:
            print(f"ОСТАВЛЕН СТАРЫЙ ({origin}): {old['title']} | {err}")
            continue
        words_old = len(" ".join([old["lead"]] + old["body"]).split())
        words_new = len(" ".join([new["lead"]] + new["body"]).split())
        old.update({"title": new["title"], "lead": new["lead"], "body": new["body"]})
        path.write_text(json.dumps(old, ensure_ascii=False, indent=1), "utf-8")
        item["title"], item["lead"] = new["title"], new["lead"]
        for a in archive:
            if a["id"] == item["id"]:
                a["title"] = new["title"]
        changed += 1
        print(f"ПЕРЕПИСАНА ({origin}, слов {words_old} → {words_new}): {new['title']}")
    g.INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
    g.ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=1), "utf-8")
    g.write_sitemap(archive)
    print(f"Переписано: {changed} из {len(index['items'])}")


if __name__ == "__main__":
    main()
