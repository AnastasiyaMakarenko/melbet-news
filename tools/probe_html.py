"""Разово, бесплатно: где на странице лежит полный текст заметки."""
import re, sys
sys.path.insert(0, ".")
import generate as g
u = sys.argv[1]
raw = g.get(u)
for word in ["Сидибе", "Калиду", "Соболев"]:
    pos = [m.start() for m in re.finditer(word, raw)]
    print(word, "встречается", len(pos), "раз, позиции:", pos[:12])
pos = [m.start() for m in re.finditer("Сидибе", raw)]
for p in pos[:6]:
    print("\n----- контекст", p)
    print(raw[max(0, p - 400): p + 600].replace("\n", " "))
