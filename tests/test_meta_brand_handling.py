from app.meta_writer import _brand_tail, _fit_title


def test_brand_first_title_does_not_treat_service_phrase_as_brand():
    title = (
        "Sovereign Standard | Web Design, SEO & "
        "Digital Marketing Agency in Calgary"
    )
    assert _brand_tail(title, "Sovereign Standard") == ""


def test_brand_suffix_is_detected_when_actually_at_end():
    title = "Web Design & SEO Agency in Calgary | Sovereign Standard"
    assert _brand_tail(title, "Sovereign Standard") == "Sovereign Standard"


def test_brand_first_live_title_does_not_force_service_phrase_suffix():
    current = (
        "Sovereign Standard | Web Design, SEO & "
        "Digital Marketing Agency in Calgary"
    )
    candidate = "Web Design & SEO Agency in Calgary | Sovereign Standard"
    result = _fit_title(candidate, _brand_tail(current, "Sovereign Standard"))
    assert "Sovereign Standard" in result
    assert "Digital Marketing Agency in Calgary" not in result
    assert len(result) <= 60
