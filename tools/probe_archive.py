"""Разовая проверка: где у источников архив новостей по дням (sitemap)."""
import re, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"}
for site in ["https://www.championat.com", "https://www.sport-express.ru", "https://www.sports.ru", "https://matchtv.ru", "https://sport24.ru", "https://www.sovsport.ru"]:
    try:
        robots = requests.get(site + "/robots.txt", headers=UA, timeout=20).text
    except Exception as e:
        print(site, "ERR", e); continue
    maps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots)
    print(f"\n== {site}: sitemap в robots.txt: {maps[:6]}")
    for m in maps[:3]:
        try:
            x = requests.get(m, headers=UA, timeout=25).text
        except Exception as e:
            print("  ", m, "ERR", e); continue
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", x)
        mods = re.findall(r"<lastmod>\s*([^<]+?)\s*</lastmod>", x)
        pub = re.findall(r"<news:publication_date>\s*([^<]+?)\s*</news:publication_date>", x)
        print(f"   {m}: {len(locs)} ссылок; примеры: {locs[:3]} ... {locs[-2:]}; lastmod: {mods[:2]}; news-dates: {pub[:2]}")
