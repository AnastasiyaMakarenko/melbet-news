"""Разовая проверка: какие RSS-ленты отдают новости серверу GitHub."""
import re, time, calendar, feedparser, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"}
SITES = {
 "sports.ru": ["https://www.sports.ru/rss/main.xml", "https://www.sports.ru/rss/all_news.xml"],
 "championat": ["https://www.championat.com/rss/news/"],
 "sport24": ["https://sport24.ru/rss", "https://sport24.ru/rss.xml", "https://sport24.ru/feed", "https://sport24.ru/"],
 "euro-football": ["https://www.euro-football.ru/rss.xml", "https://www.euro-football.ru/rss", "https://www.euro-football.ru/"],
 "sport-express": ["https://www.sport-express.ru/services/materials/news/se/", "https://www.sport-express.ru/rss/", "https://www.sport-express.ru/"],
 "matchtv": ["https://matchtv.ru/rss", "https://matchtv.ru/rss.xml", "https://matchtv.ru/news/rss", "https://matchtv.ru/news"],
 "sovsport": ["https://www.sovsport.ru/rss/index.xml", "https://www.sovsport.ru/rss", "https://www.sovsport.ru/"],
 "bookmaker-ratings": ["https://bookmaker-ratings.ru/news/categories/sportnews/feed/", "https://bookmaker-ratings.ru/feed/", "https://bookmaker-ratings.ru/news/categories/sportnews/"],
}
for site, urls in SITES.items():
    tried = list(urls)
    for u in tried:
        try:
            r = requests.get(u, headers=UA, timeout=20)
        except Exception as e:
            print(f"[{site}] {u} -> ERR {e.__class__.__name__}"); continue
        f = feedparser.parse(r.content)
        if f.entries:
            e = f.entries[0]; t = e.get("published_parsed")
            age = round((time.time() - calendar.timegm(t)) / 60) if t else None
            print(f"[{site}] OK {u} -> {len(f.entries)} записей, свежая {age} мин назад | {e.get('link')} | {e.get('title','')[:60]}")
            break
        found = re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', r.text)
        hrefs = [re.search(r'href="([^"]+)"', x).group(1) for x in found if 'href="' in x]
        print(f"[{site}] {u} -> HTTP {r.status_code}, не RSS; ссылки на RSS в коде: {hrefs[:3]}")
        for h in hrefs[:2]:
            h = requests.compat.urljoin(u, h)
            if h not in tried: tried.append(h)
    else:
        print(f"[{site}] НЕ НАЙДЕНО")
