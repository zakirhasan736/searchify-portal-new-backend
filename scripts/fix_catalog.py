from pathlib import Path

p = Path("app/catalog_data.py")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
# Find second def seed_rows
hits = [i for i, line in enumerate(lines) if line.startswith("def seed_rows")]
print("hits", hits)
if len(hits) < 2:
    raise SystemExit("expected 2 seed_rows")
start = hits[1]
# delete until PANELS =
end = start
while end < len(lines) and not lines[end].startswith("PANELS ="):
    end += 1
if end >= len(lines):
    raise SystemExit("PANELS not found")
del lines[start:end]
# also update home-projects rows
text = "".join(lines)
old = """        [
            [SITE, SITE_URL, "72%", "0.12%", "18420", "1280", "86", "28"],
            ["demo-local.example", "https://demo-local.example", "64%", "0.04%", "4200", "310", "24", "4"],
            [COMP_A + " (watch)", "https://" + COMP_A, "—", "0.21%", "31200", "2100", "140", "41"],
        ],"""
new = """        [
            [SITE, SITE_URL, "72%", "0.12%", "18420", "1280", "86", "28"],
            ["demo-local.example", "https://demo-local.example", "64%", "0.04%", "4200", "310", "24", "4"],
            ["shop-north.example", "https://shop-north.example", "68%", "0.07%", "9800", "640", "41", "9"],
            [COMP_A + " (watch)", "https://" + COMP_A, "—", "0.21%", "31200", "2100", "140", "41"],
        ],"""
if old in text:
    text = text.replace(old, new, 1)
    print("updated home-projects")
else:
    print("home-projects block not matched")
p.write_text(text, encoding="utf-8")
hits2 = [i for i, line in enumerate(text.splitlines()) if line.startswith("def seed_rows")]
print("seed_rows after", hits2)
