"""Разово: новые картинки для уже опубликованных новостей по новым правилам (живее и разнообразнее, без людей).
Картинки сохраняются под НОВЫМИ именами (<id>-v2.jpg, -v3 …) — старые у посетителей закэшированы, перезаписывать их нельзя.
Заодно убирает из ленты новости без текста."""
import json, pathlib, sys
sys.path.insert(0, ".")
import generate as g

d = pathlib.Path("data")
index = json.loads((d / "news.json").read_text("utf-8"))
archive = json.loads((d / "archive.json").read_text("utf-8"))

empty = []
for item in index["items"]:
    art_path = d / "a" / f"{item['id']}.json"
    art = json.loads(art_path.read_text("utf-8"))
    if len(art.get("body") or []) < 2:
        empty.append(item["id"]); print("без текста, убираю из ленты:", item["title"]); continue
    prompt = g.ask_json("Ты фоторедактор спортивного сайта. Верни JSON {\"image_prompt\": \"...\"}: image_prompt — " + g.IMAGE_RULES,
                        {"title": art["title"], "lead": art["lead"], "category": art["category"]})["image_prompt"]
    # новое имя файла: <id>-vN (старые картинки закэшированы у посетителей навсегда — перезаписывать нельзя)
    m = g.re.search(r"-v(\d+)\.jpg$", item.get("image") or "")
    nid = f"{item['id']}-v{int(m.group(1)) + 1 if m else 2}"
    try:
        big, small = g.make_image(prompt, nid)
    except Exception as err:
        print("не получилось, оставляю старую:", item["title"], err); continue
    for obj in (item, art):
        obj["image"], obj["thumb"] = big, small
    art_path.write_text(json.dumps(art, ensure_ascii=False, indent=1), "utf-8")
    print("новая картинка:", item["title"], "|", prompt[:120])

index["items"] = [i for i in index["items"] if i["id"] not in empty]
archive = [a for a in archive if a["id"] not in empty]
(d / "news.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
(d / "archive.json").write_text(json.dumps(archive, ensure_ascii=False, indent=1), "utf-8")
g.write_sitemap(archive)
