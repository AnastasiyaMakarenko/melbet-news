"""Разово: одни и те же новости — картинки от нескольких моделей, чтобы выбрать лучшую. В ленту ничего не попадает.
Результат: data/compare/<модель>/<id>.jpg и data/compare/index.json."""
import json, pathlib, sys

sys.path.insert(0, ".")
import generate as g

MODELS = ["openai/gpt-5-image-mini", "google/gemini-nano-banana-2.1", "openai/gpt-5.4-image-2"]
WANT = ["Футбол", "Хоккей", "Волейбол", "Фигурное катание"]

d = g.DATA_DIR
out = d / "compare"
items = json.loads(g.INDEX.read_text("utf-8"))["items"]
picked = []
for cat in WANT:                                           # по одной новости разных видов спорта
    for i in items:
        if i["category"] == cat and i not in picked:
            picked.append(i); break

result = []
for i in picked:
    art = json.loads((g.ART_DIR / f"{i['id']}.json").read_text("utf-8"))
    prompt = g.ask_json("Ты фоторедактор спортивного сайта. Верни JSON {\"image_prompt\": \"...\"} на английском: " + g.IMAGE_RULES,
                        {"title": art["title"], "lead": art["lead"], "category": art["category"]})["image_prompt"]
    row = {"id": i["id"], "title": art["title"], "prompt": prompt, "images": {}}
    for m in MODELS:
        g.IMAGE_MODEL = m
        g.IMG_DIR = out / m.split("/")[1]
        g.IMG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            big, _ = g.make_image(prompt, i["id"])
            row["images"][m] = f"compare/{m.split('/')[1]}/{i['id']}.jpg"
            print("готово:", m, "|", art["title"])
        except Exception as err:
            print("не получилось:", m, err)
    result.append(row)
(out / "index.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
