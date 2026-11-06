"""A fake public website served through httpx's MockTransport, so the real scanner code runs end to end."""
import httpx

HOST = "acme-roofing-test.com"
PAGES = {
    "/": ("Acme Roofing | Roof repair in Calgary", "Same-week roof repair and replacement across Calgary.", "Roof repair and replacement in Calgary",
          '<a href="/services/roof-repair/">Roof repair</a> <a href="/about/">About</a> <a href="/contact/">Contact</a>'),
    "/services/roof-repair/": ("Roof Repair Calgary | Acme Roofing", "Leaks fixed fast. Free inspection.", "Roof repair", '<a href="/">Home</a>'),
    "/about/": ("About Acme Roofing", "", "About us", '<a href="/">Home</a>'),
    "/contact/": ("Contact Acme Roofing", "Call or email for a free quote.", "Contact", '<a href="/">Home</a>'),
}
ROBOTS = f"User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n\nSitemap: https://{HOST}/sitemap_index.xml\n"
SITEMAP_INDEX = f"""<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://{HOST}/page-sitemap.xml</loc></sitemap></sitemapindex>"""
PAGE_SITEMAP = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
    f"<url><loc>https://{HOST}{p}</loc></url>" for p in PAGES) + "</urlset>"
LOG = []
MODE = {"robots_status": 200, "robots_block_bot": False}


def html(path):
    title, desc, h1, links = PAGES[path]
    words = " ".join(["Acme Roofing repairs leaking roofs, replaces shingles and inspects storm damage across Calgary and Airdrie."] * 8)
    meta = f'<meta name="description" content="{desc}">' if desc else ""
    return f"""<!doctype html><html lang="en"><head><title>{title}</title>{meta}
<link rel="canonical" href="https://{HOST}{path}"><meta property="og:title" content="{title}">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"RoofingContractor","name":"Acme Roofing"}}</script>
<link rel="stylesheet" href="/wp-content/themes/x/style.css"></head>
<body><header><nav>{links}</nav></header><main><h1>{h1}</h1><h2>Why us</h2><p>{words}</p>
<img src="/wp-content/uploads/roof.jpg" alt="Roofer at work" width="800" height="600"></main><footer>{links}</footer></body></html>"""


def handler(request: httpx.Request) -> httpx.Response:
    url = request.url
    LOG.append((request.headers.get("user-agent", "")[:20], str(url)))
    host = url.host.removeprefix("www.")
    if host != HOST:
        return httpx.Response(404, text="unknown host")
    if url.scheme == "http":
        return httpx.Response(301, headers={"location": str(url.copy_with(scheme="https"))})
    if url.host.startswith("www."):
        return httpx.Response(301, headers={"location": str(url.copy_with(host=HOST))})
    path = url.path
    if MODE.get("waf") and "SearchifyBot" in request.headers.get("user-agent", ""):
        return httpx.Response(403, text="<html><title>Access denied</title>Blocked by firewall</html>", headers={"content-type": "text/html"})
    if path == "/robots.txt":
        if MODE["robots_block_bot"] and "SearchifyBot" in request.headers.get("user-agent", ""):
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(MODE["robots_status"], text=ROBOTS, headers={"content-type": "text/plain"})
    if path == "/sitemap_index.xml":
        return httpx.Response(200, text=SITEMAP_INDEX, headers={"content-type": "application/xml"})
    if path == "/page-sitemap.xml":
        return httpx.Response(200, text=PAGE_SITEMAP, headers={"content-type": "application/xml"})
    if path == "/llms.txt":
        return httpx.Response(404, text="nope")
    if path in PAGES:
        return httpx.Response(200, text=html(path), headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(404, text="<html><title>Not found</title></html>", headers={"content-type": "text/html"})


TRANSPORT = httpx.MockTransport(handler)


def install():
    """Route every httpx call (sync clients and module-level helpers) to the fake site."""
    real_init = httpx.Client.__init__

    def init(self, *args, **kwargs):
        if type(self).__name__ == "TestClient":
            return real_init(self, *args, **kwargs)
        kwargs["transport"] = TRANSPORT
        kwargs.pop("proxy", None)
        kwargs.pop("proxies", None)
        real_init(self, *args, **kwargs)

    httpx.Client.__init__ = init

    def request(method, url, **kwargs):
        client_keys = {k: kwargs.pop(k) for k in ("headers", "timeout", "follow_redirects", "cookies", "verify") if k in kwargs}
        follow = client_keys.pop("follow_redirects", False)
        with httpx.Client(**client_keys) as client:
            return client.request(method, url, follow_redirects=follow, **kwargs)

    httpx.request = request
    httpx.get = lambda url, **kw: request("GET", url, **kw)
    httpx.head = lambda url, **kw: request("HEAD", url, **kw)
    httpx.post = lambda url, **kw: request("POST", url, **kw)
    from app import site_scan
    site_scan.public_host = lambda url: True
