import pytest

from app.locations import LocationError, resolve


@pytest.mark.parametrize(
    "market, iso, city, region",
    [
        ("Calgary, Alberta", "CA", "Calgary", "Alberta"),
        ("Dhaka", "BD", "Dhaka", ""),
        ("Dhaka, Bangladesh", "BD", "Dhaka", ""),
        ("Toronto", "CA", "Toronto", ""),
        ("United Kingdom", "GB", "", ""),
        ("Manchester", "GB", "Manchester", ""),
        ("Sydney, New South Wales", "AU", "Sydney", "New South Wales"),
        ("Houston, Texas", "US", "Houston", "Texas"),
        ("London, United Kingdom", "GB", "London", ""),
    ],
)
def test_resolves_market_to_country(market, iso, city, region):
    place = resolve(market)
    assert place.iso == iso
    assert place.city == city
    assert place.region == region
    assert place.language


@pytest.mark.parametrize("market", ["", "   ", "Springfield", "London", "Calgary, United States", "Canada and India"])
def test_never_defaults_to_united_states(market):
    with pytest.raises(LocationError):
        resolve(market)


def test_worldwide_needs_a_country_unless_allowed():
    with pytest.raises(LocationError):
        resolve("Worldwide")
    assert resolve("Worldwide", allow_worldwide=True).worldwide
    assert resolve("customers anywhere", reach="Worldwide", allow_worldwide=True).iso == ""


def test_named_country_wins_over_worldwide_reach():
    assert resolve("Canada", reach="Worldwide").iso == "CA"
