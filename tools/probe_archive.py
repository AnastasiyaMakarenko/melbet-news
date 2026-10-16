"""Разовая проверка: новостные sitemap источников — сколько ссылок и за какие даты."""
import re, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"}
def get(u):
    return requests.get(u, headers=UA, timeout=30).text
def show(u):
    try:
        x = get(u)
    except Exception as e:
        print("  ", u, "ERR", e); return
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", x)
    dates = re.findall(r"<(?:lastmod|news:publication_date)>\s*([^<]+?)\s*</", x)
    titles = re.findall(r"<news:title>\s*(?:<!\[CDATA\[)?([^<\]]+)", x)
    ds = sorted(d[:10] for d in dates)
    print(f"  {u}: {len(locs)} ссылок, даты {ds[:1]}..{ds[-1:]}, заголовков {len(titles)}; пример: {locs[:2]} {titles[:1]}")
show("https://www.sport-express.ru/sitemap/news.xml")
show("https://www.sport-express.ru/sitemap/lastnews.xml")
show("https://www.sovsport.ru/sitemap-news.xml")
idx = get("https://sport24.ru/sitemap_index.xml")
news = [u for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", idx) if "news" in u or "article" in u or "google" in u]
print("sport24 news-карты:", news[:8], len(news))
for u in news[:3]: show(u)
idx = get("https://matchtv.ru/sitemap.xml")
news = [u for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", idx) if "news" in u]
print("matchtv news-карты:", news[:6], len(news))
for u in news[:2]: show(u)
