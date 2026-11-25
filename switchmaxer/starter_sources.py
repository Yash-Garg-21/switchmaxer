"""Built-in employer boards available during local setup."""

from collections.abc import Mapping
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .discovery import _location_name, _profile_values


_CATALOG = Path(__file__).with_name("starter_sources.json")
_INDIA_LOCATIONS = frozenset({
    "ahmedabad", "andhra pradesh", "bengaluru", "chandigarh", "chennai", "coimbatore",
    "delhi", "delhi ncr", "goa", "greater noida", "gujarat", "gurugram", "haryana",
    "hyderabad", "india", "indore", "jaipur", "jammu and kashmir", "karnataka", "kerala",
    "kochi", "kolkata", "lucknow", "madhya pradesh", "maharashtra", "mumbai", "mysore",
    "mysuru", "navi mumbai", "ncr", "new delhi", "noida", "odisha", "pune", "punjab",
    "rajasthan", "tamil nadu", "telangana", "thiruvananthapuram", "uttar pradesh",
    "uttarakhand", "west bengal",
})


def _india_location(value: str) -> bool:
    location = _location_name(value)
    if location in _INDIA_LOCATIONS:
        return True
    return location.endswith(" india") and not re.search(r"\b(?:and|or|outside|except|excluding|not)\b", location)


def starter_sources(profile: Mapping) -> list[dict]:
    """Return fresh source configs with India-specific settings for an India-only search."""
    locations = _profile_values(profile, "locations")
    india_only = bool(locations) and all(_india_location(location) for location in locations)

    sources = []
    for entry in json.loads(_CATALOG.read_text(encoding="utf-8")):
        if entry.get("india_only") and not india_only:
            continue
        url = entry["url"]
        if india_only and entry["kind"] == "smartrecruiters":
            parts = urlsplit(url)
            filters = dict(parse_qsl(parts.query)) | {"country": "in"}
            url = parts._replace(query=urlencode(filters)).geturl()
        sources.append({
            "name": entry["name"], "kind": entry["kind"], "url": url,
            "slug": entry.get("slug", ""), "crawl_permission": entry.get("crawl_permission", False),
        })
    return sources
