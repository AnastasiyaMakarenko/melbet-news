"""Разовая проверка: открывается ли лендинг по адресам с параметром и с доп. путём."""
import asyncio
from playwright.async_api import async_playwright
URLS = ["https://melbet.ru/ru/pages/melbetpremiumsize",
        "https://melbet.ru/ru/pages/melbetpremiumsize?news=test",
        "https://melbet.ru/ru/pages/melbetpremiumsize/test",
        "https://melbet.ru/ru/news/test"]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(locale="ru-RU", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
        for u in URLS:
            page = await ctx.new_page()
            try:
                r = await page.goto(u, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(12000)
                mbn = await page.locator("#mbn").count()
                rows = await page.locator("#mbn .row, #mbn .hero").count()
                h1 = await page.locator("h1").all_inner_texts()
                body = (await page.inner_text("body"))[:200].replace("\n", " | ")
                print(f"RESULT {u}\n   http={r.status if r else None} final={page.url}\n   title={await page.title()!r} mbn={mbn} news_cards={rows} h1={h1[:3]}\n   text={body!r}")
            except Exception as e:
                print(f"RESULT {u}\n   ERR {type(e).__name__}: {str(e)[:200]}")
            await page.close()
        await b.close()
asyncio.run(main())
