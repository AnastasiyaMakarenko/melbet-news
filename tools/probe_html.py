"""Разово, бесплатно: структура страницы заметки (теги вокруг текста)."""
import re, sys
sys.path.insert(0, ".")
import generate as g
u = sys.argv[1]
raw = g.get(u)
page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
i = page.find("Семак")
j = page.find("<h1")
print("h1 at", j, "| first Семак at", i)
seg = page[j:j + 12000]
seg = re.sub(r"\s+", " ", seg)
for m in re.finditer(r"<(p|div|section|article|aside|a)\b[^>]*>", seg):
    pass
# печатаем последовательность тегов с классами и куски текста
out = re.sub(r'<(/?)(\w+)([^>]*?)(?:class="([^"]*)")?[^>]*>', lambda m: f"\n<{m.group(1)}{m.group(2)} .{(m.group(4) or '')[:40]}>", seg)
print(out[:9000])
