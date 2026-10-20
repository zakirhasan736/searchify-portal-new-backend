from io import BytesIO

from PIL import Image

from app.page_weight import compress_image, kb, page_sizes, same_site, weight_issues


def test_page_sizes_ignore_missing_fields():
    assert page_sizes({"url": "https://example.com"}) == {"images": None, "scripts": None, "css": None}
    assert page_sizes({"meta": {"images_size": "2400000", "scripts_size": 100, "stylesheets_size": 0}})["images"] == 2400000


def test_weight_issues_use_stored_bytes_only():
    quiet = {"url": "https://example.com/a", "resource_type": "html", "meta": {"scripts_count": 40, "images_count": 12}}
    heavy = {
        "url": "https://example.com/",
        "resource_type": "html",
        "meta": {"images_size": 900_000, "scripts_size": 400_000, "stylesheets_size": 20_000},
    }
    assert weight_issues([quiet]) == []
    issues = weight_issues([heavy, quiet])
    keys = [issue["key"] for issue in issues]
    assert keys == ["heavy_images", "heavy_assets"]
    assert "900 KB" in issues[0]["pages"][0]["detail"]
    assert issues[0]["prepare"] == "image"
    assert issues[1]["prepare"] == "assets"
    assert "theme" in issues[1]["fix"].lower() or "theme" in issues[1]["pages"][0]["detail"]
    assert kb(900_000) == "900 KB"


def test_same_site_rejects_other_hosts_and_private_addresses():
    page = "https://www.example.com/work"
    assert same_site(page, "/photo.jpg")
    assert same_site(page, "https://example.com/photo.jpg")
    assert not same_site(page, "https://cdn.example.net/photo.jpg")
    assert not same_site(page, "https://127.0.0.1/photo.jpg")
    assert not same_site(page, "http://192.168.0.8/photo.jpg")


def test_compress_image_makes_a_smaller_jpeg():
    image = Image.new("RGB", (1800, 1200), (20, 40, 80))
    for x in range(0, 1800, 3):
        for y in range(0, 1200, 7):
            image.putpixel((x, y), ((x * 3) % 255, (y * 5) % 255, 90))
    raw = BytesIO()
    image.save(raw, format="PNG")
    original = raw.getvalue()
    smaller, mime = compress_image(original)
    assert mime == "image/jpeg"
    assert len(smaller) <= 200_000
    assert smaller[:2] == b"\xff\xd8"
