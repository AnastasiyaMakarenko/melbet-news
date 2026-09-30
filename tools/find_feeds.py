"""Разовая проверка: как брать новости с сайтов без нормального RSS."""
import re, time, calendar, html, feedparser, requests
from collections import Counter
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"}
f = feedparser.parse(requests.get("https://www.euro-football.ru/rss.xml", headers=UA, timeout=20).content)
ts = sorted([calendar.timegm(e.published_parsed) for e in f.entries if e.get("published_parsed")], reverse=True)
print("euro-football rss newest ages (min):", [round((time.time()-t)/60) for t in ts[:5]])
for u in ["https://www.sovsport.ru/news", "https://www.sovsport.ru/", "https://bookmaker-ratings.ru/news/categories/sportnews/",
          "https://www.euro-football.ru/news", "https://www.euro-football.ru/"]:
    try:
        r = requests.get(u, headers=UA, timeout=20)
    except Exception as e:
        print(u, "ERR", e); continue
    links = re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', r.text, re.S)
    rows = [(h, re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()) for h, t in links]
    rows = [(h, t) for h, t in rows if len(t) > 35]
    pat = Counter(re.sub(r"\d+", "N", re.sub(r"https?://[^/]+", "", h)).rsplit("/", 1)[0] for h, _ in rows)
    print(f"\n== {u} HTTP {r.status_code} len={len(r.text)} links_with_text={len(rows)}")
    print("   patterns:", pat.most_common(6))
    for h, t in rows[:8]: print("   ", h[:110], "|", t[:70])
    print("   json-ld dates:", re.findall(r'"datePublished"\s*:\s*"([^"]+)"', r.text)[:3], " time tags:", re.findall(r'<time[^>]*datetime="([^"]+)"', r.text)[:3])
