"""Разово: показать, как наш рерайт перепишет присланные примеры статей (ничего не публикует).
Примеры передаются при запуске (переменная SAMPLES), в репозитории не хранятся."""
import json, os, sys

sys.path.insert(0, ".")
import generate as g

for sources in json.loads(os.environ["SAMPLES"]):
    try:
        art = g.rewrite(sources)
        ok, problems = g.factcheck(sources, art)
        problems += g.violations(art)
        if not ok or problems:
            art = g.rewrite(sources, problems)
            ok, problems = g.factcheck(sources, art)
            problems += g.violations(art)
        words = len(" ".join([art["lead"]] + art["body"]).split())
        print("=" * 100)
        print(f"[{art['category']}] {art['title']}")
        print(f"ЛИД: {art['lead']}")
        for p in art["body"]:
            print("   " + p)
        print(f"-- слов: {words}, абзацев: {len(art['body'])}, фактчек: {'ок' if ok else problems}, нарушения: {g.violations(art) or 'нет'}")
    except Exception as err:
        print("не получилось:", err)
