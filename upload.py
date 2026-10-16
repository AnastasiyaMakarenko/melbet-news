"""Выгружает готовые новости из папки data/ в облако (Cloudflare R2, ведро только для новостей).

Что уходит в облако: news.json, archive.json, sitemap.xml, статьи a/<id>.json и картинки img/<id>.jpg, img/<id>_s.jpg.
Загружаются только новые и изменившиеся файлы. Из облака ничего не удаляется.
Порядок: сначала картинки и статьи, в конце лента — чтобы лента никогда не ссылалась на файл, которого ещё нет.
"""
import hashlib, json, os, pathlib, sys

import boto3

DATA = pathlib.Path("data")
BUCKET = os.environ["R2_BUCKET"]
TYPES = {".json": "application/json; charset=utf-8", ".xml": "application/xml; charset=utf-8", ".jpg": "image/jpeg"}
# картинки не меняются никогда — браузер может хранить их год; лента и sitemap обновляются — держим минуту
CACHE_FOREVER = "public, max-age=31536000, immutable"
CACHE_SHORT = "public, max-age=60"
CACHE_ARTICLE = "public, max-age=300"


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"], region_name="auto",
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

    index = json.loads((DATA / "news.json").read_text("utf-8")).get("items", [])
    archive = json.loads((DATA / "archive.json").read_text("utf-8")) if (DATA / "archive.json").exists() else []
    ids = list(dict.fromkeys([a["id"] for a in archive] + [i["id"] for i in index]))

    plan = []                                              # (файл, ключ в облаке, кэш)
    for nid in ids:
        art_file = DATA / "a" / f"{nid}.json"
        if not art_file.exists():
            continue
        art = json.loads(art_file.read_text("utf-8"))
        # картинки — по именам, записанным в самой статье (после перегенерации имя меняется)
        for name in dict.fromkeys(x for x in (art.get("image"), art.get("thumb")) if x):
            if (DATA / name).exists():
                plan.append((DATA / name, name, CACHE_FOREVER))
        plan.append((art_file, f"a/{nid}.json", CACHE_ARTICLE))
    for name in ("archive.json", "sitemap.xml", "news.json"):   # лента — последней
        if (DATA / name).exists():
            plan.append((DATA / name, name, CACHE_SHORT))

    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for o in page.get("Contents", []):
            remote[o["Key"]] = o["ETag"].strip('"')

    sent = 0
    for path, key, cache in plan:
        body = path.read_bytes()
        if remote.get(key) == hashlib.md5(body).hexdigest():
            continue                                       # в облаке уже лежит такой же файл
        s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=TYPES[path.suffix], CacheControl=cache)
        sent += 1
        print("  загружен:", key)
    print(f"Облако: загружено {sent}, без изменений {len(plan) - sent}, всего новостей {len(ids)}")


if __name__ == "__main__":
    try:
        main()
    except KeyError as err:
        sys.exit(f"Не задана настройка облака: {err}")
