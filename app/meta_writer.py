--- searchify-portal-new-backend-main/app/meta_writer.py	2026-10-25 09:50:22.000000000 +0000
+++ fixed/app/meta_writer.py	2026-10-25 11:25:08.513444665 +0000
@@ -195,14 +195,24 @@
     return cut or text[:limit].rstrip()
 
 
-def _brand_tail(title: str) -> str:
+def _brand_tail(title: str, brand: str = "") -> str:
+    """Trailing brand segment ("Services | Brand"). Empty for brand-first titles ("Brand | Services")."""
     text = str(title or "")
+    name = " ".join(str(brand or "").split()).lower()
+    if name and text.strip().lower().startswith(name):
+        return ""
     for sep in (" | ", " – ", " — ", " - "):
         if sep in text:
             return text.split(sep)[-1].strip()
     return ""
 
 
+def lost_current_terms(title: str, current: str, brand: str = "") -> list[str]:
+    """Meaningful words the live title has that the draft dropped (brand words excluded)."""
+    keep = _content_tokens(current) - _brand_tokens(brand)
+    return sorted(keep - _content_tokens(title))
+
+
 def _keep_published(value: str, limit: int = 180) -> str:
     """Keep a live title or description. Do not shorten it to force a score."""
     return re.sub(r"\s+", " ", (value or "").strip())[:limit]
@@ -293,8 +303,8 @@
     length = len(title or "")
     if 30 <= length <= 60:
         score += 12
-    elif length > 60:
-        score -= 20
+    elif length > 60 and not (current_title and (title or "").strip().lower() == current_title.strip().lower()):
+        score -= 20  # length is a display note for drafts; the published title is not punished for it
     desc_len = len(desc or "")
     if 120 <= desc_len <= 160:
         score += 8
@@ -900,9 +910,17 @@
                 break
         if not fresh and not alt_hit:
             return False
-    tail = _brand_tail(current)
+    tail = _brand_tail(current, brand)
     if tail and tail.lower() not in (title or "").lower():
         return False
+    if current and brand and brand.lower() in current.lower() and brand.lower() not in (title or "").lower():
+        return False
+    # A draft may replace words, never just delete them (e.g. "Agency in Calgary" cut for length).
+    lost = lost_current_terms(title, current, brand)
+    if lost:
+        gained = _content_tokens(title) - _content_tokens(current)
+        if len(gained) < len(lost):
+            return False
     # Do not treat a thinner rewrite as stronger when the live title already names brand + offers.
     if current and brand and brand.lower() in current.lower():
         live_offers = sum(1 for offer in (offers or []) if _content_tokens(offer) & current_words)
@@ -1201,7 +1219,7 @@
     )
 
     empty = {
-        "title": _clean(scraped.get("title") or "", 60),
+        "title": _keep_published(scraped.get("title") or ""),
         "metaDescription": _clean(scraped.get("description") or "", 155),
         "reason": "OpenAI is not configured, so the live title was kept.",
         "model": "",
@@ -1662,7 +1680,7 @@
     kept_options: list[tuple[str, str]] = []
 
     def keep_option(cand_title: str, cand_desc: str) -> None:
-        t = _fit_title(cand_title or "", _brand_tail(current))
+        t = _fit_title(cand_title or "", _brand_tail(current, brand))
         d = _fit_desc(cand_desc or "")
         if not t or not d:
             return
@@ -1771,7 +1789,7 @@
                     "Regeneration did not produce a validated, demonstrably stronger listing."
                 )
     query_rows = _query_metric_rows(gsc, queries)
-    tail = _brand_tail(current)
+    tail = _brand_tail(current, brand)
     live_fit = ctr_fit_score(
         current, live_desc, keyword=keyword, brand_tail=tail, current_title=current, query_rows=query_rows,
     )
@@ -1867,7 +1885,7 @@
     target_kw = _parse_target(text) or keyword or ((site or {}).get("page") or {}).get("target") or ""
     # Preserve distinct social copy when recommending keep; otherwise align OG with the proposed listing.
     if recommend_keep:
-        og_title = _fit_title(str(scraped.get("ogTitle") or current or title), tail)
+        og_title = _keep_published(str(scraped.get("ogTitle") or current or title), 120)  # never truncate a published title
         og_desc = _fit_desc(str(scraped.get("ogDescription") or live_desc or desc))
     else:
         og_title, og_desc = title, desc
