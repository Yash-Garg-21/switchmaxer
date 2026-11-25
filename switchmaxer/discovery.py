"""Public job feeds, scoped company crawls, and user-provided job alerts."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
from fnmatch import fnmatchcase
from gzip import GzipFile
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import posixpath
import re
import socket
import ssl
from time import monotonic, sleep
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zlib import error as ZlibError


@dataclass(frozen=True)
class JobRecord:
    source: str
    source_id: str
    company: str
    title: str
    location: str
    url: str
    apply_url: str
    description: str
    published_date: str | None = None
    requisition_id: str = ""
    work_mode: str = ""


@dataclass
class SourceResult:
    source: dict
    records: list[JobRecord]
    status: str
    error: str = ""


BOARDS = {
    "linkedin": ("linkedin.com", "lnkd.in"),
    "naukri": ("naukri.com",),
    "instahyre": ("instahyre.com",),
}
ATS = {"greenhouse", "lever", "ashby", "smartrecruiters"}
_SMARTRECRUITERS_HOST = "api.smartrecruiters.com"
_SMARTRECRUITERS_BOARD_HOSTS = {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"}
_SMARTRECRUITERS_PAGE_SIZE = 100
_SMARTRECRUITERS_PAGE_LIMIT = 2
_SMARTRECRUITERS_DETAIL_LIMIT = 7
TRACKING = {"ref", "trk", "trackingid", "refid", "source", "gh_src", "lever-source", "li_fat_id"}
AGENT = "SwitchMaxer"
URL_PATTERN = re.compile(r"""https?://[^\s<>"\x00-\x1f]+""", re.I)
GENERIC_LINK = re.compile(r"view(?: the)? job|apply(?: now)?|see job|details|job details|learn more|click here", re.I)


def _board(host: str) -> str:
    for name, domains in BOARDS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return ""


def canonical_url(value: str) -> str:
    """Normalize public listing identities without dropping job identifiers."""
    try:
        raw = unescape(str(value or "").strip())
        if not raw or "\\" in raw or any(c.isspace() or ord(c) < 32 for c in raw):
            return ""
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return ""
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9._:\-]+", host):
            return ""
        port = parts.port
        netloc = f"[{host}]" if ":" in host else host
        if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            netloc += f":{port}"
    except (UnicodeError, ValueError):
        return ""

    path = parts.path.rstrip("/") or "/"
    query = parse_qsl(parts.query, keep_blank_values=True)
    if _board(host) == "linkedin" and (identifier := _linkedin_id(path, query)):
        return f"https://www.linkedin.com/jobs/view/{identifier}"
    if host == "boards.greenhouse.io":
        netloc = "job-boards.greenhouse.io"
    if host in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}:
        path = path.removesuffix("/apply") or "/"
    cleaned = [(key, value) for key, value in query
               if key.lower() not in TRACKING and not key.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, netloc, path, urlencode(sorted(cleaned)), ""))


def _apply_url(value: str) -> str:
    # ATS apply URLs must keep their /apply suffix, unlike canonical job identities.
    if not canonical_url(value):
        return ""
    return unescape(str(value).strip())


def _linkedin_id(path: str, query: list[tuple[str, str]]) -> str:
    match = re.search(r"/jobs/view/(?:[^/]*-)?(\d{4,})(?:/|$)", path, re.I)
    if match:
        return match[1]
    if re.search(r"/jobs(?:/search|/collections|/view)?/?$", path):
        identifier = dict(query).get("currentJobId", "")
        return identifier if identifier.isascii() and identifier.isdigit() else ""
    return ""


def _short(value) -> str:
    return " ".join(str(value or "").split())


def _date(value) -> str | None:
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
        if value:
            return date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


class _Page(HTMLParser):
    """Read visible text, anchor labels, and embedded structured postings."""

    blocks = {
        "article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr",
    }

    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self._hidden = ""
        self._script: list[str] | None = None
        self._anchor: tuple[str, list[str]] | None = None
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden = tag
            script_type = (attributes.get("type") or "").lower().split(";")[0].strip()
            if tag == "script" and script_type == "application/ld+json":
                self._script = []
        elif not self._hidden:
            if tag in self.blocks:
                self.lines.append("\n")
            if tag == "a" and attributes.get("href"):
                self._anchor = (attributes["href"], [])

    def handle_endtag(self, tag):
        if tag == self._hidden:
            self._hidden = ""
            if self._script is not None:
                self.scripts.append("".join(self._script))
                self._script = None
        elif not self._hidden:
            if tag in self.blocks:
                self.lines.append("\n")
            if tag == "a" and self._anchor is not None:
                href, label = self._anchor
                self.links.append((href, _short("".join(label))))
                self._anchor = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif not self._hidden:
            self.lines.append(data)
            if self._anchor is not None:
                self._anchor[1].append(data)

    def text(self) -> str:
        return "\n".join(filter(None, map(_short, "".join(self.lines).splitlines())))


def _plain(value) -> str:
    text = str(value or "")
    expanded = unescape(text)
    if re.search(r"</?[a-z][^>]*>", expanded, re.I):
        return _Page(expanded).text()
    return expanded.strip()


class _FetchError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


_FEED_ERRORS = (_FetchError, OSError, EOFError, ZlibError, ValueError, http.client.HTTPException)


def _public_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Pin each outbound connection to a resolved public address."""
    endpoints = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_error = None
    for family, kind, protocol, _, address in endpoints:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast:
            continue
        sock = socket.socket(family, kind, protocol)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    if last_error:
        raise last_error
    raise _FetchError("The source must resolve to a public address.")


def _get(url: str, *, allowed=None, redirect_delay: float = 0, max_mib: int = 12) -> tuple[bytes, str]:
    for hop in range(5):
        if not canonical_url(url) or (allowed is not None and not allowed(url)):
            raise _FetchError("The requested URL is outside the configured source.")
        if hop and redirect_delay:
            sleep(redirect_delay)
        parts = urlsplit(url)
        host = parts.hostname.encode("idna").decode("ascii")
        secure = parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        if port != (443 if secure else 80):
            raise _FetchError("Public sources must use standard web ports.")
        timeout = 8.0
        connection = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(
            host, port, timeout=timeout,
        )
        sock = _public_socket(host, port, timeout)
        try:
            connection.sock = sock
            if secure:
                connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            request_path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("GET", request_path, headers={"User-Agent": f"{AGENT}/1.0", "Accept-Encoding": "gzip"})
            with connection.getresponse() as response:
                redirect = response.getheader("Location")
                if redirect and response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, redirect)
                    continue
                if response.status != 200:
                    raise _FetchError(f"HTTP {response.status} from {host}.", response.status)
                limit = max_mib * 1024 * 1024
                if response.getheader("Content-Encoding", "").lower() == "gzip":
                    with GzipFile(fileobj=response) as compressed:
                        body = compressed.read(limit + 1)
                else:
                    body = response.read(limit + 1)
                if len(body) > limit:
                    raise _FetchError(f"The source response exceeds {max_mib} MiB.")
                return body, url
        finally:
            connection.close()
    raise _FetchError("The source redirected too many times.")


def _feed_endpoint(kind: str, source: dict) -> str:
    url = urlsplit(str(source.get("url") or ""))
    pieces = [part for part in url.path.split("/") if part]
    if kind == "smartrecruiters":
        return _smartrecruiters_endpoint(source, url, pieces)
    prefix = {"greenhouse": ["v1", "boards"], "lever": ["v0", "postings"],
              "ashby": ["posting-api", "job-board"]}[kind]
    domains = {"greenhouse": "greenhouse.io", "lever": "lever.co", "ashby": "ashbyhq.com"}
    host = url.hostname or ""
    if pieces[:2] == prefix:
        pieces = pieces[2:]
    hosted = (host == domains[kind] or host.endswith("." + domains[kind]))
    slug = str(source.get("slug") or (pieces[0] if hosted and pieces else "")).strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", slug) or slug in {".", ".."}:
        raise ValueError("Supply the board slug or a hosted job-board URL.")
    if kind == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if kind == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    host = "api.eu.lever.co" if (url.hostname or "").endswith(".eu.lever.co") else "api.lever.co"
    return f"https://{host}/v0/postings/{slug}?mode=json"


def _smartrecruiters_endpoint(source: dict, url: SplitResult, pieces: list[str]) -> str:
    hosted_slug = ""
    if pieces and url.hostname in _SMARTRECRUITERS_BOARD_HOSTS:
        hosted_slug = pieces[0]
    elif (url.hostname == _SMARTRECRUITERS_HOST and len(pieces) in {4, 5}
          and pieces[:2] == ["v1", "companies"] and pieces[3] == "postings"):
        hosted_slug = pieces[2]
    slug = _short(source.get("slug") or hosted_slug)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", slug) or slug in {".", ".."}:
        raise ValueError("Supply a SmartRecruiters board slug or a hosted board URL.")
    query = dict(parse_qsl(url.query))
    country = _short(query.get("country")).lower()
    if country and not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Use a two-letter country code for SmartRecruiters, for example country=in.")
    filters = [(key, value) for key, value in (("country", country), ("q", _short(query.get("q")))) if value]
    filters.append(("destination", "PUBLIC"))
    return urlunsplit(("https", _SMARTRECRUITERS_HOST, f"/v1/companies/{slug}/postings", urlencode(filters), ""))


_UNSPECIFIED_LOCATION = re.compile(
    r"(?:hybrid|remote|on[\s-]?site|in[\s-]+office)(?:\s+(?:work|mode|model|only))?"
    r"|(?:n/?a|tbd|unknown|unspecified|other|headquarters|global|all offices|multiple locations)",
    re.I,
)
_LOCATION_LINE = re.compile(r"^(?:(?:available|job|work|office)\s+)?locations?\s*(?:[:|–—-]\s*(.*))?$", re.I)


def _specific_location(value) -> str:
    place = _short(value) if isinstance(value, str) else ""
    return place if place and not _UNSPECIFIED_LOCATION.fullmatch(place) else ""


def _greenhouse_location(item: dict, description: str) -> str:
    # Some employers send only a work mode in the location field. Prefer a
    # location explicitly attached to the posting or labeled in its description.
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    for index, line in enumerate(lines[:45]):
        if not (label := _LOCATION_LINE.fullmatch(line)):
            continue
        place = _specific_location(label[1])
        if place:
            return place
        if label[1]:
            continue
        places = []
        for following in lines[index + 1:index + 9]:
            following = following.strip("• ")
            words = re.findall(r"[^\W\d_]+", following)
            if (len(following) > 120 or not 0 < len(words) <= 12
                    or any(not word[0].isupper() and word not in {"of", "in", "the", "and", "or", "de"} for word in words)
                    or re.search(r"\b(?:about|requirements|responsibilities|team|experience|role)\b", following, re.I)
                    or places and not re.search(r"[,|;]", following)):
                break
            place = _specific_location(following)
            if not place:
                break
            places.append(place)
        if places:
            return "; ".join(dict.fromkeys(places))
    offices = []
    for office in item.get("offices") or []:
        if isinstance(office, dict):
            place = _specific_location(office.get("location")) or _specific_location(office.get("name"))
            if place:
                offices.append(place)
    return "; ".join(dict.fromkeys(offices))


def _feed_job(kind: str, company: str, item: dict) -> JobRecord | None:
    original_mode = ""
    if kind == "greenhouse":
        title, key = item.get("title"), item.get("id")
        url = apply_url = item.get("absolute_url")
        location = (item.get("location") or {}).get("name")
        description = _plain(item.get("content"))
        original_mode = _work_mode(location or "")
        if not _specific_location(location):
            location = _greenhouse_location(item, description) or location
        published = item.get("first_published")
    elif kind == "lever":
        title, key = item.get("text"), item.get("id")
        url, apply_url = item.get("hostedUrl"), item.get("applyUrl")
        categories = item.get("categories") or {}
        location = "; ".join(categories.get("allLocations") or []) or categories.get("location")
        location = ", ".join(filter(None, (location, item.get("country"))))
        paragraphs = [item.get("descriptionPlain") or item.get("description")]
        paragraphs.extend(
            "\n".join(filter(None, (_plain(section.get("text")), _plain(section.get("content")))))
            for section in item.get("lists") or []
        )
        paragraphs.append(item.get("additionalPlain") or item.get("additional"))
        description = "\n".join(_plain(paragraph) for paragraph in paragraphs if paragraph)
        published = item.get("createdAt")
    else:
        if item.get("isListed") is False:
            return None
        title, key = item.get("title"), item.get("id")
        url, apply_url = item.get("jobUrl"), item.get("applyUrl")
        places = [item.get("location")]
        places.extend(place.get("location") for place in item.get("secondaryLocations") or [])
        location = "; ".join(dict.fromkeys(filter(None, places)))
        description = _plain(item.get("descriptionPlain") or item.get("descriptionHtml"))
        published = item.get("publishedAt")

    deadline = _date(item.get("application_deadline"))
    canonical = canonical_url(url)
    if not title or not canonical or (deadline and deadline < datetime.now(timezone.utc).date().isoformat()):
        return None
    mode = _short(item.get("workplaceType"))
    return JobRecord(
        kind, str(key or canonical), company, _short(title), _short(location), canonical,
        _apply_url(apply_url) or canonical, description, _date(published),
        _short(item.get("requisition_id") or item.get("requisitionId")),
        _work_mode(mode) or original_mode or _work_mode(f"{title} {location or ''}"),
    )


def _feed(source: dict, kind: str) -> list[JobRecord]:
    endpoint = _feed_endpoint(kind, source)
    host = urlsplit(endpoint).hostname
    payload, _ = _get(endpoint, allowed=lambda url: urlsplit(url).hostname == host, max_mib=48)
    response = json.loads(payload)
    items = response if kind == "lever" else response.get("jobs") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("The public feed did not return a jobs list.")
    jobs = [_feed_job(kind, str(source["name"]), item) for item in items if isinstance(item, dict)]
    return [job for job in jobs if job is not None]


def _smartrecruiters_open(item: dict) -> bool:
    deadline = _date(item.get("unpublishOn"))
    return (item.get("active") is not False
            and _short(item.get("visibility")).upper() != "INTERNAL"
            and (not deadline or deadline >= datetime.now(timezone.utc).date().isoformat()))


def _smartrecruiters_record(slug: str, company: str, item: dict) -> JobRecord:
    key = _short(item["id"])
    place = item.get("location")
    place = place if isinstance(place, dict) else {}
    location = _short(place.get("fullLocation")) or ", ".join(
        filter(None, (_short(place.get(field)) for field in ("city", "region", "country"))),
    )
    location = ", ".join(part.strip() for part in location.split(",") if part.strip())
    title = _short(item["name"])
    if place.get("remote") is True:
        mode = "remote"
    elif place.get("hybrid") is True:
        mode = "hybrid"
    else:
        mode = _work_mode(f"{title} {location}")
    ad = item.get("jobAd")
    sections = ad.get("sections") if isinstance(ad, dict) else None
    sections = sections if isinstance(sections, dict) else {}
    descriptions = []
    for field, label in (("jobDescription", "Job description"), ("qualifications", "Qualifications"),
                         ("additionalInformation", "Additional information")):
        section = sections.get(field)
        if isinstance(section, dict) and (body := _plain(section.get("text"))):
            descriptions.append(f"{_short(section.get('title')) or label}\n{body}")
    url = canonical_url(item.get("postingUrl")) or f"https://jobs.smartrecruiters.com/{slug}/{key}"
    return JobRecord(
        "smartrecruiters", f"{slug}/{key}", company, title, location, url, _apply_url(item.get("applyUrl")) or url,
        "\n\n".join(descriptions), _date(item.get("releasedDate")), _short(item.get("refNumber")), mode,
    )


def _smartrecruiters_feed(source: dict) -> SourceResult:
    endpoint = urlsplit(_feed_endpoint("smartrecruiters", source))
    slug = endpoint.path.split("/")[3]
    filters = parse_qsl(endpoint.query)
    company = str(source["name"])

    def fetch(path: str, query: list[tuple[str, str]] | None = None) -> dict:
        request = endpoint._replace(path=path, query=urlencode(query or ()))

        def allowed(candidate: str) -> bool:
            return urlsplit(candidate) == request

        payload, _ = _get(request.geturl(), allowed=allowed, max_mib=4)
        response = json.loads(payload)
        if not isinstance(response, dict):
            raise ValueError("The SmartRecruiters feed did not return an object.")
        return response

    listings = {}
    notes: list[str] = []
    offset = 0
    more = True
    details_blocked = False
    for _ in range(_SMARTRECRUITERS_PAGE_LIMIT):
        try:
            page = fetch(endpoint.path, filters + [("limit", str(_SMARTRECRUITERS_PAGE_SIZE)), ("offset", str(offset))])
            rows = page.get("content")
            if not isinstance(rows, list):
                raise ValueError("The SmartRecruiters feed did not return a postings list.")
        except _FEED_ERRORS as error:
            if not offset:
                raise
            notes.append(f"Additional SmartRecruiters postings could not be loaded. {error}")
            details_blocked = isinstance(error, _FetchError) and error.status in {401, 403, 429}
            break
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = _short(item.get("id"))
            if re.fullmatch(r"[a-zA-Z0-9-]+", key) and _short(item.get("name")) and _smartrecruiters_open(item):
                listings.setdefault(key, item)
        offset += len(rows)
        total = page.get("totalFound")
        more = bool(rows) and (offset < total if isinstance(total, int) else len(rows) >= _SMARTRECRUITERS_PAGE_SIZE)
        if not more:
            break
    else:
        if more:
            notes.append(f"SmartRecruiters listing limit reached after {offset} postings.")

    failed_details = 0
    records = []
    for index, (key, item) in enumerate(listings.items()):
        if index < _SMARTRECRUITERS_DETAIL_LIMIT and not details_blocked:
            try:
                detail = fetch(f"{endpoint.path}/{key}")
                if _short(detail.get("id")) != key:
                    raise ValueError("The SmartRecruiters detail did not match the posting ID.")
                if not _smartrecruiters_open(detail):
                    continue
                item = item | {name: value for name, value in detail.items() if value is not None}
            except _FEED_ERRORS as error:
                status = error.status if isinstance(error, _FetchError) else None
                if status in {404, 410}:
                    continue
                failed_details += 1
                details_blocked = status in {401, 403, 429}
                if details_blocked:
                    notes.append(f"SmartRecruiters stopped serving posting details. {error}")
        records.append(_smartrecruiters_record(slug, company, item))

    if len(listings) > _SMARTRECRUITERS_DETAIL_LIMIT:
        notes.append(f"Up to {_SMARTRECRUITERS_DETAIL_LIMIT} posting descriptions are fetched per refresh.")
    if failed_details:
        notes.append(f"Details could not be loaded for {failed_details} SmartRecruiters postings.")
    if without_description := sum(not job.description for job in records):
        notes.append(f"{without_description} postings have a link and basic details only.")
    return SourceResult(source, records, "success", " ".join(notes))


def refresh_sources(sources: list[dict]) -> list[SourceResult]:
    """Refresh registry entries concurrently and keep each entry's outcome."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(_refresh_one, sources))


def _refresh_one(source: dict) -> SourceResult:
    kind = str(source.get("kind") or "").strip().lower()
    try:
        if kind == "smartrecruiters":
            return _smartrecruiters_feed(source)
        if kind in ATS:
            return SourceResult(source, _feed(source, kind), "success")
        if kind == "html":
            return _crawl(source)
        return SourceResult(source, [], "manual", "Import links or alerts for this source.")
    except _FEED_ERRORS + (AttributeError, KeyError, TypeError) as error:
        return SourceResult(source, [], "failed", str(error))


def _job_postings(value):
    if isinstance(value, list):
        for item in value:
            yield from _job_postings(item)
    elif isinstance(value, dict):
        kinds = value.get("@type") or []
        if isinstance(kinds, str):
            kinds = [kinds]
        if "JobPosting" in kinds:
            yield value
        for field in ("@graph", "itemListElement", "item"):
            yield from _job_postings(value.get(field))


def _structured_job(item: dict, page: str, source: dict) -> JobRecord | None:
    deadline = _date(item.get("validThrough"))
    title = _short(item.get("title"))
    if not title or (deadline and deadline < datetime.now(timezone.utc).date().isoformat()):
        return None
    url = canonical_url(urljoin(page, str(item.get("url") or ""))) or canonical_url(page)
    organization = item.get("hiringOrganization")
    company = (organization.get("name") if isinstance(organization, dict) else organization)
    identifier = item.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    locations = item.get("jobLocation") or []
    if isinstance(locations, (str, dict)):
        locations = [locations]
    places = []
    for location in locations:
        address = location.get("address", location) if isinstance(location, dict) else location
        if isinstance(address, dict):
            parts = [address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry")]
            address = ", ".join(_short(part.get("name") if isinstance(part, dict) else part)
                                for part in parts if part)
        if address:
            places.append(_short(address))
    if item.get("jobLocationType") == "TELECOMMUTE":
        remote = item.get("applicantLocationRequirements") or []
        if isinstance(remote, dict):
            remote = [remote]
        allowed_locations = [_short(place.get("name")) for place in remote if isinstance(place, dict)]
        places.extend(place for place in allowed_locations if place)
        places.append("Remote")
    description = _plain(item.get("description"))
    requirement = item.get("experienceRequirements")
    if isinstance(requirement, dict) and (months := requirement.get("monthsOfExperience")):
        try:
            requirement = f"{float(months) / 12:g} years of experience"
        except (TypeError, ValueError):
            requirement = ""
    if isinstance(requirement, str) and requirement.strip() and requirement not in description:
        description = f"{requirement.strip()}\n{description}".strip()
    location = "; ".join(dict.fromkeys(places))
    return JobRecord(
        "html", url, _short(company) or source["name"], title, location, url, _apply_url(url),
        description, _date(item.get("datePosted")), _short(identifier), _work_mode(location),
    )


def _robots(origin: str) -> tuple[list[tuple[str, bool]], float]:
    robots_url = origin + "/robots.txt"
    try:
        text, _ = _get(robots_url, allowed=lambda url: urlsplit(url).netloc == urlsplit(origin).netloc)
    except _FetchError as error:
        if error.status not in {404, 410}:
            raise
        return [], 0

    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    directives: list[tuple[str, str]] = []
    for line in text[:512_000].decode("utf-8-sig", errors="replace").splitlines():
        key, _, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if directives:
                groups.append((agents, directives))
                agents, directives = [], []
            agents.append(value.lower())
        elif agents and key in {"allow", "disallow", "crawl-delay", "request-rate"}:
            directives.append((key, value))
    groups.append((agents, directives))
    selected = [rules for names, rules in groups if AGENT.lower() in names]
    if not selected:
        selected = [rules for names, rules in groups if "*" in names]

    rules = []
    delay = 0.0
    for directives in selected:
        for key, value in directives:
            if key in {"allow", "disallow"} and value.startswith("/"):
                rules.append((unquote(value), key == "allow"))
            elif key == "crawl-delay" and re.fullmatch(r"\d{1,6}(?:\.\d+)?", value):
                delay = max(delay, float(value))
            elif key == "request-rate" and (rate := re.fullmatch(r"([1-9]\d{0,5})\s*/\s*(\d{1,6})", value)):
                delay = max(delay, int(rate[2]) / int(rate[1]))
    return rules, delay


def _robots_allows(rules: list[tuple[str, bool]], url: str) -> bool:
    parts = urlsplit(url)
    target = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
    matches = []
    for rule, allow in rules:
        pattern = rule.removesuffix("$").replace("[", "[[]").replace("?", "[?]")
        if not rule.endswith("$"):
            pattern += "*"
        if fnmatchcase(target, pattern):
            matches.append((len(rule.rstrip("$").replace("*", "")), allow))
    return max(matches, default=(0, True))[1]


def _crawl(source: dict) -> SourceResult:
    start = canonical_url(source.get("url"))
    parts = urlsplit(start)
    if _board(parts.hostname or ""):
        return SourceResult(source, [], "manual", "Import this job board from links or alerts.")
    if not source.get("crawl_permission"):
        return SourceResult(source, [], "manual", "Confirm crawling is permitted for this career site.")
    if not start:
        raise ValueError("Supply a public careers URL.")

    origin = f"{parts.scheme}://{parts.netloc}"
    scope = posixpath.normpath(unquote(parts.path)).rstrip("/")
    rules, requested_delay = _robots(origin)

    def allowed(url: str) -> bool:
        target = urlsplit(url)
        path = posixpath.normpath(unquote(target.path))
        return ((target.scheme, target.netloc) == (parts.scheme, parts.netloc)
                and (path == scope or path.startswith(scope + "/"))
                and _robots_allows(rules, url))

    if not allowed(start):
        return SourceResult(source, [], "manual", "The career URL is outside the permitted robots.txt scope.")
    delay = max(0.25, requested_delay)
    if delay > 10:
        return SourceResult(source, [], "manual", "The site asks for a crawl interval longer than 10 seconds.")

    pending = deque([start])
    visited = {start}
    records = {}
    pages = 0
    stop_at = monotonic() + 35
    while pending and pages < 28 and monotonic() < stop_at:
        url = pending.popleft()
        sleep(delay)
        try:
            body, final_url = _get(url, allowed=allowed, redirect_delay=delay)
        except _FetchError as error:
            if error.status in {404, 410} and url != start:
                continue
            return SourceResult(source, list(records.values()), "failed", str(error))
        except (OSError, http.client.HTTPException) as error:
            return SourceResult(source, list(records.values()), "failed", str(error))
        pages += 1
        page = _Page(body.decode("utf-8", errors="replace"))
        for script in page.scripts:
            try:
                value = json.loads(script.strip())
            except (ValueError, RecursionError):
                continue
            for item in _job_postings(value):
                job = _structured_job(item, final_url, source)
                if job:
                    records[job.url] = job
        for href, label in page.links:
            link = canonical_url(urljoin(final_url, href))
            if (link and link not in visited and allowed(link)
                    and re.search(r"job|role|openings?|positions?|vacancies|careers?", f"{urlsplit(link).path} {label}", re.I)):
                visited.add(link)
                pending.append(link)
    if not records:
        return SourceResult(source, [], "manual", "No JobPosting data found in the bounded crawl. Import job links instead.")
    note = "Crawl limit reached. Additional pages may be available." if pending else ""
    return SourceResult(source, list(records.values()), "success", note)


def _alert_body(content: str | bytes) -> tuple[str, list[tuple[str, str]], bool]:
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    is_email = any(message.get(key) for key in ("From", "Subject", "MIME-Version", "Content-Type"))
    if not is_email:
        text = raw.decode("utf-8", errors="replace").strip()
        if re.search(r"<(?:html|body|p|a)\b", text, re.I):
            page = _Page(text)
            return page.text(), page.links, False
        return text, [], False
    parts = [part for part in message.walk()
             if part.get_content_type() in {"text/plain", "text/html"} and part.get_content_disposition() != "attachment"]
    visible, links = [], []
    for part in parts:
        try:
            text = part.get_content()
        except (KeyError, UnicodeError):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            page = _Page(text)
            visible.append(page.text())
            links.extend(page.links)
        else:
            visible.append(text)
    return "\n".join(dict.fromkeys(visible)), links, True


def _alert_job(source: str, raw_url: str) -> bool:
    url = canonical_url(raw_url)
    parts = urlsplit(url)
    if not url or _board(parts.hostname or "") != source:
        return False
    if source == "linkedin":
        return bool(_linkedin_id(parts.path, parse_qsl(parts.query)))
    if source == "naukri":
        return bool(re.search(r"/(?:job-listings?[-/]|jd/|job/)", parts.path, re.I))
    return bool(re.search(r"/(?:job[-/]|opportunity[-/])[^/]+", parts.path, re.I))


IMPORT_LABELS = {
    "company": r"company|employer",
    "title": r"role|title|job title",
    "location": r"location",
    "apply_url": r"apply url|apply link|link to apply|application link|employer link",
    "published_date": r"date posted|posted date|published date",
    "requisition_id": r"requisition id|job id",
}


def _import_fields(text: str) -> dict[str, str]:
    fields = {}
    for field, labels in IMPORT_LABELS.items():
        match = re.search(rf"^(?:{labels})[ \t]*:[ \t]*(.+)$", text, re.I | re.M)
        if match:
            fields[field] = match[1].strip()
    return fields


def _link_identity(raw_url: str, label: str) -> tuple[str, str]:
    if GENERIC_LINK.fullmatch(label):
        label = ""
    if label and (parts := re.split(r"\s+at\s+", label, maxsplit=1, flags=re.I)) and len(parts) == 2:
        return parts[0], parts[1]
    path = unquote(urlsplit(raw_url).path)
    slug = re.search(r"/jobs/view/([^/]+?)-at-([^/]+?)-\d{4,}(?:/|$)", path, re.I)
    if slug:
        return label or slug[1].replace("-", " ").title(), slug[2].replace("-", " ").title()
    return label, ""


def _nearby_company(text: str, title: str) -> tuple[str, str]:
    if not title:
        return "", ""
    lines = [_short(line) for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == title.casefold():
            company, *parts = re.split(r"\s*[·•]\s*", lines[index + 1])
            if parts and len(company) <= 100:
                return company, parts[0]
    return "", ""


def _import_title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first) <= 110 and re.search(
        r"\b(engineer|analyst|developer|manager|lead|designer|scientist|specialist|associate|writer|consultant)\b",
        first, re.I,
    ) and not URL_PATTERN.search(first):
        return first
    return ""


def parse_import(
    source: str,
    content: str | bytes = "",
    *,
    url: str = "",
    company: str = "",
    title: str = "",
    location: str = "",
    description: str = "",
    apply_url: str = "",
    published_date: str = "",
    requisition_id: str = "",
) -> list[JobRecord]:
    """Convert job links, pasted JDs, or native alert files without network access."""
    source = source.strip().lower()
    if source not in {*BOARDS, "manual"}:
        raise ValueError("Choose LinkedIn, Naukri, Instahyre, or Manual.")
    text, anchors, is_email = _alert_body(content)
    fields = _import_fields(text)
    if not is_email and "title" not in fields:
        fields["title"] = _import_title(text)
    provided = url.strip()
    if provided and not canonical_url(provided):
        raise ValueError("Enter a valid HTTP or HTTPS job link.")
    if apply_url.strip() and not _apply_url(apply_url):
        raise ValueError("Enter a valid HTTP or HTTPS application link.")
    links: dict[str, tuple[str, str]] = {}
    if provided:
        links[canonical_url(provided)] = (provided, "")
    else:
        for raw, label in [(value.rstrip(".,)>;]"), "") for value in URL_PATTERN.findall(text)] + anchors:
            normalized = canonical_url(raw)
            if not normalized or (source in BOARDS and not _alert_job(source, raw)):
                continue
            if normalized not in links:
                links[normalized] = (raw, label)
            elif label and not GENERIC_LINK.fullmatch(label):
                old_raw, _ = links[normalized]
                links[normalized] = (old_raw if _link_identity(old_raw, "")[1] else raw, label)
    single = len(links) <= 1
    if not links:
        if not (title.strip() or fields.get("title")):
            return []
        links[""] = ("", "")
    records = []
    for normalized, (raw, label) in links.items():
        if len(records) >= 100:
            break
        inferred_title, inferred_company = _link_identity(raw, label)
        role = _short(title or (fields.get("title") if single else "") or inferred_title)
        nearby_company, nearby_place = _nearby_company(text, role)
        place = _short(location or (fields.get("location") if single else "") or nearby_place)
        employer = _short(company or (fields.get("company") if single else "") or inferred_company or nearby_company)
        if not role:
            label = {"linkedin": "LinkedIn", "naukri": "Naukri", "instahyre": "Instahyre"}.get(source, "Imported")
            role = f"{label} job link"
        body = _plain(description or (text if single and not is_email else ""))
        destination = _apply_url(apply_url or (fields.get("apply_url", "") if single else ""))
        destination = destination or (normalized if source == "linkedin" and _alert_job(source, raw) else _apply_url(raw))
        fields_key = f"{employer}\n{role}\n{place}\n{body}"
        records.append(JobRecord(
            source, normalized or sha256(fields_key.encode()).hexdigest(), employer, role,
            place, normalized, destination, body[:60_000],
            _date(published_date or (fields.get("published_date") if single else None)),
            _short(requisition_id or (fields.get("requisition_id") if single else "")), _work_mode(f"{role} {place}"),
        ))
    return records


_NUMBER = r"\d{1,2}(?:\.\d+)?"
_YEARS = r"(?:years?|yrs?)"
_RANGE = re.compile(rf"(?<![\d.])({_NUMBER})\s*(?:{_YEARS}\s*)?(?:[-–—]|\bto\b)\s*({_NUMBER})\s*{_YEARS}\b", re.I)
_SINGLE = re.compile(rf"(?<![\d.])(?:(up to|at most|no more than)\s+)?({_NUMBER})\s*\+?\s*{_YEARS}\b", re.I)


def experience_range(text: str) -> tuple[float | None, float | None]:
    """Return the likely overall experience requirement, keeping open bounds."""
    candidates = []
    for line in str(text or "").splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            match = _RANGE.search(sentence)
            if match:
                bounds = (float(match[1]), float(match[2]))
                if bounds[0] > bounds[1]:
                    continue
            elif match := _SINGLE.search(sentence):
                bounds = (None, float(match[2])) if match[1] else (float(match[2]), None)
            else:
                continue
            if re.search(r"founded|in business|years? (?:old|ago)|anniversary", sentence, re.I):
                continue
            if re.search(r"preferred|bonus|nice to have", sentence, re.I) and not re.search(
                r"required|minimum|must have", sentence, re.I,
            ):
                continue
            rank = 1 + bool(re.search(r"\bexperience\b", sentence, re.I))
            candidates.append((rank, bounds))
    return max(candidates, key=lambda item: item[0])[1] if candidates else (None, None)


def _work_mode(text: str) -> str:
    for mode, pattern in {
        "hybrid": r"\bhybrid\b",
        "remote": r"\bremote\b|\bwork from home\b",
        "onsite": r"\bon[\s-]?site\b|\bin[\s-]office\b",
    }.items():
        if re.search(pattern, text or "", re.I):
            return mode
    return ""


_LEVELS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "sr": "senior",
           "jr": "junior", "entry": "junior", "internship": "intern", "midlevel": "mid"}
_LEVEL_TOKENS = {"1", "2", "3", "4", "5", "intern", "junior", "mid", "senior", "staff", "principal", "lead"}
_LEVEL_BANDS = {"1": "junior", "2": "mid", "3": "senior", "4": "staff", "5": "principal"}
_SOFTWARE_ROLE = re.compile(r"\b(?:sde|swe|software (?:development |dev )?engineer|software developer)(?:\s*([1-5]|i{1,3}|iv|v))?\b")


def _words(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", str(value or "").casefold()))


def _title_parts(title: str) -> tuple[set[str], set[str]]:
    name = _words(title)
    name = _SOFTWARE_ROLE.sub(lambda match: f"software engineer {match[1] or ''}".rstrip(), name)
    name = re.sub(r"\b(?:entry level|mid level)\b", lambda match: match[0].split()[0], name)
    tokens = {_LEVELS.get(word, word) for word in name.split()} - {"level", "of", "in"}
    return tokens - _LEVEL_TOKENS, tokens & _LEVEL_TOKENS


def _profile_values(profile: Mapping, key: str) -> list[str]:
    value = profile.get(key) or []
    choices = re.split(r"[,;\n]", value) if isinstance(value, str) else value
    return [choice.strip() for choice in choices if str(choice).strip()]


def _location_name(text: str) -> str:
    value = _words(re.sub(r"\b(?:IN|IND)\b", "India", text))
    for alias, name in {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai",
                        "us": "united states", "usa": "united states", "uk": "united kingdom"}.items():
        value = re.sub(rf"\b{alias}\b", name, value)
    return value


def _company_name(text: str) -> str:
    return re.sub(r"\s+(?:pvt|private|ltd|limited|inc|llc|corp|corporation)\b", "", _words(text)).strip()


def matches_profile(job: JobRecord | Mapping, profile: Mapping) -> bool:
    """Apply explicit preferences while retaining listings with unknown metadata."""
    def value(name: str) -> str:
        if isinstance(job, JobRecord):
            return getattr(job, name)
        return str(job.get(name) or "")

    employer = _company_name(value("company"))
    include = {_company_name(name) for name in _profile_values(profile, "include_companies")}
    exclude = {_company_name(name) for name in _profile_values(profile, "exclude_companies")}
    if employer and (employer in exclude or (include and employer not in include)):
        return False

    title = value("title")
    words, levels = _title_parts(title)
    unknown_title = not title or title.endswith(" job link")
    targets = _profile_values(profile, "target_titles")
    if not targets and profile.get("current_role"):
        targets = [profile["current_role"]]
    if targets and not unknown_title and not any(_title_parts(target)[0] <= words for target in targets):
        return False
    requested_levels = set().union(*(_title_parts(level)[1] for level in _profile_values(profile, "target_levels")))
    if requested_levels and levels and requested_levels.isdisjoint(levels):
        equivalent = {_LEVEL_BANDS.get(level, level) for level in requested_levels}
        if equivalent.isdisjoint(_LEVEL_BANDS.get(level, level) for level in levels):
            return False

    location = _location_name(value("location"))
    locations = [_location_name(name) for name in _profile_values(profile, "locations")]
    anywhere = {"remote", "worldwide", "anywhere", "remote worldwide", "remote anywhere"}
    if locations and location and location not in anywhere:
        if not any(name and f" {name} " in f" {location} " for name in locations):
            return False
    current_mode = value("work_mode") or _work_mode(f"{title} {location}")
    modes = {_work_mode(mode) for mode in _profile_values(profile, "work_modes")} - {""}
    if current_mode and modes and current_mode not in modes:
        return False

    try:
        years = float(profile["experience_years"])
    except (KeyError, TypeError, ValueError):
        return True
    minimum, maximum = experience_range(value("description"))
    return (minimum is None or years >= minimum) and (maximum is None or years <= maximum)
