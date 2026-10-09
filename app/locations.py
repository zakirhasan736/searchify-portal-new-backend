"""One place that turns the owner's market answer into the location each provider needs.

Never defaults silently. If the market text does not name a country we can recognise, or names
two countries, the caller gets a LocationError with a question the UI can show.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


class LocationError(Exception):
    """The market answer is missing or ambiguous. The message is safe to show to the owner."""


@dataclass(frozen=True)
class Location:
    country: str
    iso: str
    language: str
    city: str = ""
    region: str = ""
    worldwide: bool = False
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        if self.worldwide:
            return "Worldwide"
        return ", ".join(part for part in (self.city, self.region, self.country) if part)


COUNTRIES: dict[str, tuple[str, str, str]] = {
    "canada": ("Canada", "CA", "en"),
    "united states": ("United States", "US", "en"),
    "united states of america": ("United States", "US", "en"),
    "usa": ("United States", "US", "en"),
    "u.s.": ("United States", "US", "en"),
    "america": ("United States", "US", "en"),
    "united kingdom": ("United Kingdom", "GB", "en"),
    "uk": ("United Kingdom", "GB", "en"),
    "great britain": ("United Kingdom", "GB", "en"),
    "england": ("United Kingdom", "GB", "en"),
    "scotland": ("United Kingdom", "GB", "en"),
    "wales": ("United Kingdom", "GB", "en"),
    "northern ireland": ("United Kingdom", "GB", "en"),
    "ireland": ("Ireland", "IE", "en"),
    "australia": ("Australia", "AU", "en"),
    "new zealand": ("New Zealand", "NZ", "en"),
    "india": ("India", "IN", "en"),
    "bangladesh": ("Bangladesh", "BD", "en"),
    "pakistan": ("Pakistan", "PK", "en"),
    "singapore": ("Singapore", "SG", "en"),
    "south africa": ("South Africa", "ZA", "en"),
    "germany": ("Germany", "DE", "de"),
    "france": ("France", "FR", "fr"),
    "spain": ("Spain", "ES", "es"),
    "italy": ("Italy", "IT", "it"),
    "netherlands": ("Netherlands", "NL", "nl"),
    "mexico": ("Mexico", "MX", "es"),
    "brazil": ("Brazil", "BR", "pt"),
    "united arab emirates": ("United Arab Emirates", "AE", "en"),
    "uae": ("United Arab Emirates", "AE", "en"),
}

REGIONS: dict[str, tuple[str, str]] = {
    # Canada
    "alberta": ("Alberta", "canada"), "british columbia": ("British Columbia", "canada"),
    "ontario": ("Ontario", "canada"), "quebec": ("Quebec", "canada"), "manitoba": ("Manitoba", "canada"),
    "saskatchewan": ("Saskatchewan", "canada"), "nova scotia": ("Nova Scotia", "canada"),
    "new brunswick": ("New Brunswick", "canada"), "newfoundland": ("Newfoundland and Labrador", "canada"),
    "prince edward island": ("Prince Edward Island", "canada"),
    # United States
    "california": ("California", "united states"), "texas": ("Texas", "united states"),
    "florida": ("Florida", "united states"), "new york state": ("New York", "united states"),
    "illinois": ("Illinois", "united states"), "washington state": ("Washington", "united states"),
    "colorado": ("Colorado", "united states"), "arizona": ("Arizona", "united states"),
    "georgia usa": ("Georgia", "united states"), "massachusetts": ("Massachusetts", "united states"),
    "ohio": ("Ohio", "united states"), "michigan": ("Michigan", "united states"),
    "pennsylvania": ("Pennsylvania", "united states"), "new jersey": ("New Jersey", "united states"),
    "north carolina": ("North Carolina", "united states"), "virginia": ("Virginia", "united states"),
    "oregon": ("Oregon", "united states"), "nevada": ("Nevada", "united states"),
    "utah": ("Utah", "united states"), "minnesota": ("Minnesota", "united states"),
    # Australia
    "new south wales": ("New South Wales", "australia"), "victoria australia": ("Victoria", "australia"),
    "queensland": ("Queensland", "australia"),
}

CITIES: dict[str, str] = {
    "calgary": "canada", "edmonton": "canada", "toronto": "canada", "vancouver": "canada",
    "montreal": "canada", "ottawa": "canada", "winnipeg": "canada", "mississauga": "canada",
    "red deer": "canada", "lethbridge": "canada", "airdrie": "canada", "okotoks": "canada",
    "surrey": "canada", "burnaby": "canada", "halifax": "canada", "regina": "canada", "saskatoon": "canada",
    "new york city": "united states", "los angeles": "united states", "chicago": "united states",
    "houston": "united states", "phoenix": "united states", "dallas": "united states",
    "san francisco": "united states", "seattle": "united states", "miami": "united states",
    "denver": "united states", "atlanta": "united states", "boston": "united states",
    "manchester": "united kingdom", "birmingham uk": "united kingdom", "glasgow": "united kingdom",
    "edinburgh": "united kingdom", "leeds": "united kingdom", "liverpool": "united kingdom",
    "dublin": "ireland", "sydney": "australia", "melbourne": "australia", "brisbane": "australia",
    "auckland": "new zealand", "wellington": "new zealand",
    "dhaka": "bangladesh", "chittagong": "bangladesh", "chattogram": "bangladesh", "sylhet": "bangladesh",
    "khulna": "bangladesh", "rajshahi": "bangladesh",
    "mumbai": "india", "delhi": "india", "new delhi": "india", "bangalore": "india", "bengaluru": "india",
    "karachi": "pakistan", "lahore": "pakistan", "dubai": "united arab emirates",
}

# Names that belong to more than one country: the owner must add the country.
AMBIGUOUS_CITIES = {"london", "birmingham", "cambridge", "perth", "victoria", "georgia", "hamilton", "kingston", "richmond", "windsor", "washington"}

WORLDWIDE = re.compile(r"\b(worldwide|global|globally|international|anywhere|all countries)\b", re.I)


def _find(text: str, names) -> list[str]:
    return [name for name in names if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text)]


ISO_BY_CODE = {meta[1]: (meta[0], meta[1], meta[2]) for meta in COUNTRIES.values()}


def resolve(market: str, *, reach: str = "", allow_worldwide: bool = False) -> Location:
    """Turn 'Calgary, Alberta' or 'Dhaka' into a Location. Raises LocationError rather than guessing."""
    text = " ".join((market or "").lower().replace("\u00a0", " ").split())
    if not text:
        raise LocationError("Add the city, region or country you serve in setup so results use the right market.")
    # Accept bare ISO codes (CA, GB) from the shared market selector.
    iso_token = text.strip().upper()
    if re.fullmatch(r"[A-Z]{2}", iso_token) and iso_token in ISO_BY_CODE:
        name, iso, language = ISO_BY_CODE[iso_token]
        return Location(country=name, iso=iso, language=language, source="market:iso")
    if WORLDWIDE.search(text) or (reach or "").strip().lower() == "worldwide" and not _find(text, COUNTRIES):
        if allow_worldwide:
            return Location(country="", iso="", language="en", worldwide=True, source="market:worldwide")
        raise LocationError(
            "Your market is worldwide. Choose one country to start with so search data uses a real location."
        )

    region_keys = _find(text, REGIONS)
    city_keys = [k for k in _find(text, CITIES) if not any(k != other and k in other for other in _find(text, CITIES))]
    rest = text
    for key in [*region_keys, *city_keys]:
        rest = rest.replace(key, " ")
    country_keys = _find(rest, COUNTRIES)
    countries = {COUNTRIES[k][1]: COUNTRIES[k] for k in country_keys}

    implied = {COUNTRIES[REGIONS[k][1]][1] for k in region_keys} | {COUNTRIES[CITIES[k]][1] for k in city_keys}
    if len(countries) > 1:
        raise LocationError(
            f"“{market}” names more than one country ({', '.join(v[0] for v in countries.values())}). Choose one to start with."
        )
    if countries:
        name, iso, language = next(iter(countries.values()))
        if implied and iso not in implied:
            raise LocationError(f"“{market}” mixes places from different countries. Check the market in setup.")
        source = "market:country"
    elif len(implied) == 1:
        iso = next(iter(implied))
        name, _, language = next(v for v in COUNTRIES.values() if v[1] == iso)
        source = "market:region" if region_keys else "market:city"
    elif len(implied) > 1:
        raise LocationError(f"“{market}” could be in more than one country. Add the country, for example “{market}, Canada”.")
    else:
        hits = _find(text, AMBIGUOUS_CITIES)
        if hits:
            raise LocationError(f"“{hits[0].title()}” exists in more than one country. Add the country, for example “{hits[0].title()}, United Kingdom”.")
        raise LocationError(
            f"We could not tell which country “{market}” is in. Add the country to your market in setup, for example “{market}, Canada”."
        )

    region = REGIONS[region_keys[0]][0] if region_keys else ""
    city = ""
    if city_keys:
        city = city_keys[0].title()
    elif not region_keys:
        first = (market or "").split(",")[0].strip()
        if first and first.lower() not in COUNTRIES:
            city = first
    return Location(country=name, iso=iso, language=language, city=city, region=region, source=source)
