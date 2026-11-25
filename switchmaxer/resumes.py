"""Local resume matching, skills-only LaTeX tailoring, and PDF validation."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

import pdfplumber


TAILORING_REVISION = 3


class ResumeError(ValueError):
    """A resume or source project could not be read or verified."""


class _SkillsLayoutError(ResumeError):
    """A candidate's skills block could not fit the verified PDF layout."""


@dataclass(frozen=True)
class PdfInfo:
    text: str
    email: str
    pages: int
    name: str = ""
    phone: str = ""
    profile_url: str = ""
    github_url: str = ""


@dataclass(frozen=True)
class ResumeProposal:
    resume_id: int
    resume_name: str
    pdf_path: str
    sha256: str
    tailored: bool = False
    additions: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    warning: str = ""
    reordered: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Skill:
    name: str
    category: str
    aliases: tuple[str, ...]


# These terms help recognize requirements. Only user-confirmed skills can be added.
_SKILLS = {
    "Languages": """
    Java|Java SE; C++|C++17|C++20|C++23|CPP; C#|C Sharp; C; Python|Python 3; JavaScript|ECMAScript|JS;
    TypeScript|TS; Go|Golang; Rust; Kotlin; Swift; Ruby; PHP; Scala; R; SQL; Bash|Shell scripting;
    PowerShell; HTML|HTML5; CSS|CSS3; Lua; Perl; Dart; Objective-C; MATLAB; Fortran; COBOL
    """,
    "Technical Skills": """
    Data Structures and Algorithms|Data Structure and Algorithms|DSA; Distributed Systems;
    Microservices|Microservice Architecture; API Design and Development|API Design|API Development;
    Object-Oriented Programming|Object Oriented Programming|OOP; Operating Systems; Database Management Systems|DBMS;
    Competitive Programming; REST APIs|RESTful APIs|REST API|RESTful; GraphQL; gRPC; WebSockets;
    System Design; Software Architecture; Event-Driven Architecture|Event Driven Architecture;
    React|React.js|ReactJS; React Native; Next.js|NextJS; Vue.js|VueJS|Vue; Angular|AngularJS;
    Node.js|NodeJS; Express.js|ExpressJS; NestJS|Nest.js; Svelte; FastAPI; Flask; Django; Spring Boot;
    Spring Framework; .NET|Dotnet|.NET Core; ASP.NET; Ruby on Rails|Rails; Laravel; Flutter;
    Linux Kernel; TCP/IP; Networking; HTTP; DNS; BGP; Routing and Switching; Network Security;
    Integration Testing; Unit Testing; Test Automation; Agile; Scrum; Accessibility|WCAG;
    Embedded Systems; RTOS; Data Engineering; Data Analysis; Statistics; A/B Testing; ETL; ELT;
    Data Modeling|Data Modelling; Performance Optimization; Concurrency; Multi-threading|Multithreading
    """,
    "Databases": """
    PostgreSQL|Postgres; MySQL; MongoDB; Redis; SQLite; Oracle Database|Oracle DB; SQL Server|MSSQL;
    DynamoDB; Cassandra|Apache Cassandra; Elasticsearch|Elastic Search; OpenSearch; Snowflake;
    BigQuery; ClickHouse; database replication; NoSQL; Firestore; MariaDB
    """,
    "AI/ML": """
    Machine Learning|ML; Artificial Intelligence|AI; Deep Learning; Natural Language Processing|NLP;
    Large Language Models|LLM|LLMs; Generative AI|GenAI; Computer Vision; LangGraph; LangChain;
    Model Context Protocol|MCP; Transformers; PyTorch; MLflow; Claude; ChatGPT; Cursor IDE;
    TensorFlow; scikit-learn|sklearn; Hugging Face; Vector Databases; Prompt Engineering;
    Retrieval-Augmented Generation|Retrieval Augmented Generation|RAG; NumPy; pandas; MLOps; OpenCV
    """,
    "Tools and Technologies": """
    Linux; Git; GitHub; GitLab; Perforce; AWS|Amazon Web Services; Azure|Microsoft Azure;
    Google Cloud|GCP|Google Cloud Platform; VS Code|Visual Studio Code; IntelliJ IDEA|IntelliJ;
    GitHub Copilot; Docker; Kubernetes|K8s; Terraform; Ansible; Helm; CI/CD|Continuous Integration|Continuous Delivery;
    Jenkins; GitHub Actions; Kafka|Apache Kafka; RabbitMQ; Spark|Apache Spark; Airflow|Apache Airflow;
    Databricks; Nginx; Grafana; Prometheus; OpenTelemetry; Splunk; Datadog; VMware; Verilog;
    Cypress; Playwright; Selenium; pytest; Jest; JUnit; Postman; Jira; Figma; Excel; Tableau; Power BI
    """,
}
_BUILTINS = tuple(
    _Skill(parts[0], category, tuple(parts[1:]))
    for category, text in _SKILLS.items()
    for row in text.split(";")
    if (parts := [item.strip() for item in row.split("|") if item.strip()])
)
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,5})[ .-]?\d{3,5}[ .-]?\d{3,5}(?!\d)")
_TEXT_ARGS = {"x_tolerance": 1, "y_tolerance": 2}


def read_pdf(path: str | Path | BinaryIO) -> PdfInfo:
    """Extract local text and optional applicant suggestions without OCR."""
    try:
        with pdfplumber.open(path) as document:
            if not document.pages or len(document.pages) > 30:
                raise ResumeError("Upload a PDF resume with 1 to 30 pages.")
            text = "\n".join(page.extract_text(**_TEXT_ARGS) or "" for page in document.pages)
            urls = [entry["uri"] for entry in document.pages[0].hyperlinks if isinstance(entry.get("uri"), str)]
            pages = len(document.pages)
    except ResumeError:
        raise
    except Exception as error:
        raise ResumeError("The PDF could not be read. Check that it opens and is unlocked.") from error

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = "\n".join(lines[:6])
    email = _EMAIL.search(next((unquote(url[7:]) for url in urls if url.lower().startswith("mailto:")), "") or text)
    phone_link = next((unquote(url[4:]) for url in urls if url.lower().startswith("tel:")), "")
    phone = _PHONE.search(phone_link or header)
    for match in re.finditer(r"https?://[^\s<>]+", header):
        urls.append(match.group().rstrip(".,;)"))

    first = lines[0] if lines else ""
    pieces = first.split()
    name = first if 2 <= len(pieces) <= 6 and len(first) <= 90 and all(
        any(char.isalpha() for char in piece) and all(char.isalpha() or char in ".'’-‐" for char in piece)
        for piece in pieces
    ) else ""
    return PdfInfo(
        text=text,
        email=email.group() if email else "",
        pages=pages,
        name=name,
        phone=phone.group() if phone else "",
        profile_url=_profile_url(urls, "linkedin.com", "/in/"),
        github_url=_profile_url(urls, "github.com", "/"),
    )


def _profile_url(urls: list[str], domain: str, path_prefix: str) -> str:
    for url in urls:
        try:
            path = urlsplit(url)
            host = path.hostname or ""
        except ValueError:
            continue
        if (
            path.scheme in {"https", "http"} and not path.username
            and (host == domain or host.endswith("." + domain))
            and path.path.startswith(path_prefix) and path.path != path_prefix
        ):
            return path._replace(query="", fragment="").geturl()
    return ""


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _skill_entries(value: str | Sequence[str]) -> list[list[str]]:
    entries = re.split(r"[,;\n]", value) if isinstance(value, str) else value
    return [parts for entry in entries if (
        parts := [part.strip() for part in entry.split("|") if 0 < len(part.strip()) <= 90]
    )]


def _catalog(verified: str | Sequence[str]) -> tuple[tuple[_Skill, ...], set[str]]:
    skills = {skill.name: skill for skill in _BUILTINS}
    aliases = {_normalize(alias): skill.name for skill in _BUILTINS for alias in (skill.name, *skill.aliases)}
    confirmed = set()
    for name, *extra_aliases in _skill_entries(verified):
        canonical = aliases.get(_normalize(name), name)
        skill = skills.get(canonical, _Skill(canonical, "Relevant Skills", ()))
        additional = tuple(alias for alias in extra_aliases if aliases.get(_normalize(alias), canonical) == canonical)
        names = tuple(dict.fromkeys((*skill.aliases, *additional)))
        skills[canonical] = _Skill(canonical, skill.category, names)
        aliases.update({_normalize(alias): canonical for alias in (canonical, *additional)})
        confirmed.add(canonical)
    return tuple(skills.values()), confirmed


@lru_cache(maxsize=1024)
def _skill_pattern(skill: _Skill) -> re.Pattern:
    names = sorted({_normalize(alias) for alias in (skill.name, *skill.aliases)}, key=len, reverse=True)
    alternates = "|".join(re.escape(name).replace(r"\ ", r"\s+") for name in names)
    return re.compile(rf"(?<![\w+#])(?:{alternates})(?![\w+#])")


def _find_skills(text: str, skills: tuple[_Skill, ...]) -> list[str]:
    matches = []
    for skill in skills:
        match = _skill_pattern(skill).search(text)
        if match:
            matches.append((match.start(), match.end(), skill.name))
    return [name for _, _, name in sorted(matches)]


def suggest_skills(text: str, verified_skills: str | Sequence[str] = "", *, unconfirmed_only=False) -> list[str]:
    """Return detected skills for the applicant to verify."""
    skills, confirmed = _catalog(verified_skills)
    detected = _find_skills(_normalize(text), skills)
    return [skill for skill in detected if skill not in confirmed] if unconfirmed_only else detected


_PREFERRED = re.compile(
    r"\b(?:preferred(?: qualifications| skills| requirements)?|nice[- ]to[- ]have|"
    r"desirable|bonus(?: points| skills)?|a plus|advantageous|ideally|optional)\b"
)
_REQUIRED = re.compile(
    r"^(?:(?:minimum|basic|required|essential|mandatory|must[- ]have)\b|"
    r"(?:skills|requirements|responsibilities|what you(?:'ll| will) (?:do|need))\s*:)"
)
_PREFERRED_HEADING = re.compile(
    r"^(?:preferred(?: qualifications| skills| requirements)?|nice[- ]to[- ]have|"
    r"desirable(?: skills)?|bonus(?: points| skills)?|optional(?: skills)?)\s*(?:[:–—-]|$)"
)
_SKILL_LIST = re.compile(r"^\s*(?:(?:required|preferred|technical|key|essential)\s+)?skills\s*:\s*(.+)$", re.I)
_SKILL_ITEM = re.compile(r"[\w.+#/-]+(?: [\w.+#/-]+){0,3}")


def _job_catalog(description: str, verified: str | Sequence[str]) -> tuple[tuple[_Skill, ...], set[str]]:
    skills, confirmed = _catalog(verified)
    listed = {}
    for line in description.splitlines():
        match = _SKILL_LIST.match(line.lstrip("•-* "))
        if not match:
            continue
        for item in re.split(r"[,;]|\s+(?:and|or)\s+", match.group(1), flags=re.I):
            name = " ".join(item.strip(" .").split())
            normalized = _normalize(name)
            if len(name) > 1 and _SKILL_ITEM.fullmatch(name) and not _find_skills(normalized, skills):
                listed.setdefault(normalized, _Skill(name, "Relevant Skills", ()))
    return skills + tuple(listed.values()), confirmed


def _weighted_skills(description: str, skills: tuple[_Skill, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    weight = 2
    for line in re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", description):
        normalized = _normalize(line).lstrip("•*–—- ")
        if not normalized:
            continue
        if _PREFERRED_HEADING.match(normalized):
            weight = 1
        elif _REQUIRED.match(normalized) or normalized in {"qualifications", "responsibilities"}:
            weight = 2

        for clause in re.split(r";|\bbut\b|,\s+with\b", normalized):
            importance = 1 if _PREFERRED.search(clause) else weight
            for name in _find_skills(clause, skills):
                result[name] = max(result.get(name, 0), importance)
    return result


def job_skills(description: str, verified_skills: str | Sequence[str] = "") -> dict[str, int]:
    """Map locally recognizable job skills to required (2) or preferred (1)."""
    skills, _ = _job_catalog(description, verified_skills)
    return _weighted_skills(description, skills)


_SKILLS_ENV = re.compile(r"(\\begin\{cvskills\})(.*?)(\\end\{cvskills\})", re.DOTALL)
_SECTION_TITLE = re.compile(r"\\cvsection\s*\{([^{}\\]+)\}")
_PLAIN_TEX = r"(?:[^{}\\]|\\[&#%_{}$])+"
_ROW = re.compile(rf"\\cvskill\s*\{{({_PLAIN_TEX})\}}\s*\{{({_PLAIN_TEX})\}}")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_TEX_SPECIAL = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}
_SOURCE_EXTENSIONS = {
    ".tex", ".cls", ".sty", ".cfg", ".def", ".fd", ".clo", ".ltx", ".bib", ".bst",
    ".png", ".jpg", ".jpeg", ".pdf", ".ttf", ".otf", ".eps",
}


@dataclass(frozen=True)
class _SkillRow:
    label: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class _Section:
    title: str
    source: str
    body_start: int
    body_end: int
    rows: tuple[_SkillRow, ...]

    def render(self, rows: tuple[_SkillRow, ...]) -> str:
        content = "\n".join(
            f"  \\cvskill{{{_tex(row.label)}}}{{{', '.join(_tex(value) for value in row.values)}}}"
            for row in rows
        )
        return self.source[:self.body_start] + "\n" + content + "\n" + self.source[self.body_end:]


def _tex(value: str) -> str:
    return "".join(_TEX_SPECIAL.get(character, character) for character in value)


def _unescape(value: str) -> str:
    return " ".join(re.sub(r"\\([&#%_{}$])", r"\1", value).split())


def _parse_section(source: str) -> _Section:
    # Keep offsets aligned with the original source while ignoring comments.
    uncommented = _COMMENT.sub(lambda match: " " * len(match.group()), source)
    blocks = list(_SKILLS_ENV.finditer(uncommented))
    headings = list(_SECTION_TITLE.finditer(uncommented))
    if len(blocks) != 1 or len(headings) != 1 or headings[0].end() > blocks[0].start():
        raise ResumeError("Use a Russell skills file with one \\cvsection and one cvskills block.")
    block = blocks[0]
    body = block.group(2)
    rows = tuple(
        _SkillRow(_unescape(label), tuple(_unescape(item) for item in csv.split(",") if item.strip()))
        for label, csv in _ROW.findall(body)
    )
    if not rows or _ROW.sub("", body).strip() or any(not row.values for row in rows):
        raise ResumeError("Each skills row must use \\cvskill{label}{comma-separated skills} with plain text.")
    return _Section(headings[0].group(1).strip(), source, block.start(2), block.end(2), rows)


def _paths(source_dir: str | Path, entry_file: str | Path, skills_file: str | Path) -> tuple[Path, Path, Path]:
    root = Path(source_dir).expanduser().resolve()
    if not root.is_dir() or root in {Path.home().resolve(), Path(root.anchor)}:
        raise ResumeError("Choose a local folder that contains the LaTeX project.")

    files = []
    for value in (entry_file, skills_file):
        relative = Path(value)
        if relative.is_absolute() or any(part in {".", ".."} or part.startswith(".") for part in relative.parts):
            raise ResumeError("Enter LaTeX file paths relative to the selected project folder.")
        target = root / relative
        if target.suffix.lower() != ".tex" or not target.is_file() or target.resolve() != target:
            raise ResumeError("Choose existing .tex files inside the project folder.")
        files.append(relative)
    if files[0] == files[1]:
        raise ResumeError("Choose a skills section file separate from the entry file.")
    return root, files[0], files[1]


def validate_source(source_dir: str | Path, entry_file: str | Path, skills_file: str | Path) -> None:
    """Check that the chosen project and supported skills section are readable."""
    try:
        root, _, skills = _paths(source_dir, entry_file, skills_file)
        if (root / skills).stat().st_size > 128 * 1024:
            raise ResumeError("The skills section file is too large.")
        _parse_section((root / skills).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise ResumeError("The LaTeX skills section could not be read as UTF-8.") from error


def _ranked_rows(
    rows: tuple[_SkillRow, ...],
    requirements: dict[str, int],
    skills: tuple[_Skill, ...],
    confirmed: set[str],
) -> tuple[_SkillRow, ...]:
    relevant_skills = tuple(skill for skill in skills if skill.name in requirements)
    present = {name for row in rows for value in row.values for name in _find_skills(_normalize(value), relevant_skills)}
    additions = [skill for skill in relevant_skills if skill.name in confirmed and skill.name not in present]
    if not present and not additions:
        return rows

    ranked: list[tuple[str, list[tuple[int, str]]]] = []
    for row in rows:
        items = []
        for value in row.values:
            weight = max((requirements[name] for name in _find_skills(_normalize(value), relevant_skills)), default=0)
            items.append((weight, value))
        ranked.append((row.label, items))
    for skill in sorted(additions, key=lambda item: -requirements[item.name]):
        index = next(
            (index for index, (label, _) in enumerate(ranked) if _normalize(label) == _normalize(skill.category)),
            None,
        )
        if index is None:
            index = next((i for i, (label, _) in enumerate(ranked) if label == "Relevant Skills"), None)
        if index is None:
            index = len(ranked)
            ranked.append(("Relevant Skills", []))
        ranked[index][1].append((requirements[skill.name], skill.name))

    return tuple(
        _SkillRow(label, tuple(value for _, value in sorted(items, key=lambda item: -item[0])))
        for label, items in ranked
    )


def _trimmed_variants(
    original: tuple[_SkillRow, ...],
    full: tuple[_SkillRow, ...],
    relevant: tuple[_Skill, ...],
    *,
    description: str = "",
) -> list[tuple[_SkillRow, ...]]:
    expanded = {
        index for index, (before, after) in enumerate(zip(original, full))
        if len(after.values) > len(before.values)
    }
    if not expanded and len(full) == len(original):
        return []

    counts = Counter(value for row in original for value in row.values)
    available = [max(0, len(row.values) - 2) for row in original]
    job_text = _normalize(description)
    choices = [
        (index, value)
        for index, row in enumerate(original) if available[index]
        for value in row.values
        if counts[value] == 1 and not _find_skills(_normalize(value), relevant)
        and not _skill_pattern(_Skill(value, "", ())).search(job_text)
    ]
    choices.sort(key=lambda item: (item[0] not in expanded, -len(item[1])))

    rows = list(full)
    variants = []
    for index, value in choices:
        if not available[index]:
            continue
        row = rows[index]
        rows[index] = _SkillRow(row.label, tuple(item for item in row.values if item != value))
        available[index] -= 1
        variants.append(tuple(rows))
        if len(variants) == 2:
            break
    return variants


def _changes(before: tuple[_SkillRow, ...], after: tuple[_SkillRow, ...]) -> tuple[list[str], list[str], list[str]]:
    old = dict.fromkeys(item for row in before for item in row.values)
    new = dict.fromkeys(item for row in after for item in row.values)
    additions = list(item for item in new if item not in old)
    removals = list(item for item in old if item not in new)
    changed_order = []
    for row, current in zip(before, after):
        old_order = [item for item in row.values if item in current.values]
        new_order = [item for item in current.values if item in row.values]
        if old_order != new_order:
            changed_order.append(f"{row.label}: {', '.join(old_order)} → {', '.join(new_order)}")
    return additions, removals, changed_order


def _clone(root: Path, clone: Path) -> str:
    fingerprint = hashlib.sha256()
    count = total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        subpath = here.relative_to(root)
        dirs[:] = sorted(
            item for item in dirs
            if not item.startswith(".") and not (here / item).is_symlink() and len(subpath.parts) < 8
        )
        for name in sorted(files):
            source = here / name
            if name.startswith(".") or source.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            if source.is_symlink() or not source.is_file():
                continue
            count += 1
            total += source.stat().st_size
            if count > 500 or total > 60 * 1024 * 1024:
                raise ResumeError("Choose a smaller LaTeX project folder.")
            data = source.read_bytes()
            relative = subpath / name
            destination = clone / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            fingerprint.update(relative.as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
    return fingerprint.hexdigest()


_OVERFLOW = re.compile(r"Overfull \\([hv])box \(([0-9.]+)pt too (?:wide|high)\)([^\n]*)")


def _compile(executable: str, source: Path, entry: Path, output: Path) -> tuple[Path, dict[tuple[str, str], list[float]]]:
    output.mkdir()
    try:
        result = subprocess.run(
            [executable, "--untrusted", "--only-cached", "--keep-logs", "--outdir", str(output), "--", str(entry)],
            cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ResumeError("LaTeX compilation timed out.") from error
    pdf = output / f"{entry.stem}.pdf"
    log = output / f"{entry.stem}.log"
    if result.returncode or not pdf.is_file() or not log.is_file():
        raise ResumeError("Tectonic could not compile the project using its local TeX cache.")
    overflows: dict[tuple[str, str], list[float]] = {}
    for kind, amount, context in _OVERFLOW.findall(log.read_text(encoding="utf-8", errors="replace")):
        overflows.setdefault((kind, context.strip()), []).append(float(amount))
    return pdf, overflows


def _alnum(text: str) -> Counter:
    return Counter(char for char in _normalize(text) if char.isalnum())


def _compact(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


@dataclass(frozen=True)
class _Layout:
    pages: tuple[tuple[float, float], ...]
    outside_skills: tuple[str, ...]
    outside_links: tuple[tuple[str, ...], ...]
    clipping: tuple[float, ...]
    section_overlap: float


def _layout(
    path: Path, title: str, rows: tuple[_SkillRow, ...], *, expected_pages: tuple[tuple[float, float], ...] | None = None,
) -> _Layout:
    # A Russell row may wrap with the label vertically centered across the two lines.
    # Compare the rendered skill block's character inventory independent of extraction order.
    expected = _alnum(" ".join(word for row in rows for word in (row.label, *row.values)))
    outside, links, clipping = [], [], []
    located = False
    overlap = 0.0
    try:
        with pdfplumber.open(path) as document:
            pages = tuple((round(page.width, 1), round(page.height, 1)) for page in document.pages)
            if expected_pages is not None and pages != expected_pages:
                raise _SkillsLayoutError("The tailored PDF changed its page count or dimensions.")
            for page in document.pages:
                lines = page.extract_text_lines(return_chars=False, **_TEXT_ARGS)
                links.append(tuple(sorted(entry["uri"] for entry in page.hyperlinks if entry.get("uri"))))
                clipping.append(max(
                    (max(0, -item["x0"], -item["top"], item["x1"] - page.width, item["bottom"] - page.height)
                     for item in page.chars if item["text"].strip()),
                    default=0,
                ))
                for index, line in enumerate(lines):
                    if _compact(line["text"]).casefold() != _compact(title).casefold():
                        continue
                    if located:
                        raise ResumeError("The PDF has more than one matching skills heading.")
                    characters: Counter = Counter()
                    for end in range(index + 1, len(lines)):
                        characters.update(_alnum(lines[end]["text"]))
                        if characters == expected:
                            if end + 1 < len(lines):
                                overlap = max(0, lines[end]["bottom"] - lines[end + 1]["top"])
                            lines = lines[:index + 1] + lines[end + 1:]
                            located = True
                            break
                        if characters - expected:
                            break
                    if not located:
                        raise ResumeError("The rendered PDF does not match the expected skills section.")
                    break
                outside.append(_compact(" ".join(line["text"] for line in lines)))
    except ResumeError:
        raise
    except Exception as error:
        raise ResumeError("The compiled PDF layout could not be checked.") from error
    if not located:
        raise ResumeError("A distinct skills section could not be found in the PDF.")
    return _Layout(pages, tuple(outside), tuple(links), tuple(clipping), overlap)


def _check_variant(baseline: _Layout, variant: _Layout, old_boxes: dict, new_boxes: dict) -> None:
    if variant.pages != baseline.pages:
        raise _SkillsLayoutError("The tailored PDF changed its page count or dimensions.")
    if variant.outside_skills != baseline.outside_skills or variant.outside_links != baseline.outside_links:
        raise ResumeError("The tailored PDF changed text or links outside the skills section.")
    if variant.section_overlap > baseline.section_overlap + 0.1 or any(
        new > old + 0.1 for new, old in zip(variant.clipping, baseline.clipping)
    ):
        raise _SkillsLayoutError("The tailored PDF has overlapping or clipped text.")
    for context, amounts in new_boxes.items():
        old = sorted(old_boxes.get(context, ()), reverse=True)
        new = sorted(amounts, reverse=True)
        if len(new) > len(old) or any(after > before + 0.01 for after, before in zip(new, old)):
            raise _SkillsLayoutError("The tailored PDF introduced or worsened a LaTeX overflow warning.")


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _tailor(
    row: Mapping,
    original: Path,
    original_hash: str,
    skills: tuple[_Skill, ...],
    confirmed: set[str],
    requirements: dict[str, int],
    cache: Path,
    *,
    description: str = "",
) -> tuple[Path, str, list[str], list[str], list[str]]:
    root, entry, skills_file = _paths(row["source_dir"], row.get("entry_file", ""), row.get("skills_file", ""))
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="switchmaxer-tex-") as scratch:
        stage = Path(scratch)
        clone = stage / "project"
        fingerprint = _clone(root, clone)
        editable = clone / skills_file
        if not editable.is_file() or not (clone / entry).is_file() or editable.stat().st_size > 128 * 1024:
            raise ResumeError("The local LaTeX project could not be staged.")
        source = _parse_section(editable.read_text(encoding="utf-8"))
        rows = _ranked_rows(source.rows, requirements, skills, confirmed)
        if rows == source.rows:
            return original, original_hash, [], [], []
        relevant = tuple(skill for skill in skills if skill.name in requirements)
        variants = [rows, *_trimmed_variants(source.rows, rows, relevant, description=description)]
        renderings = [source.render(variant) for variant in variants]
        key = hashlib.sha256(
            "\0".join((
                f"v{TAILORING_REVISION}", fingerprint, original_hash, str(entry), str(skills_file), *renderings,
            )).encode()
        ).hexdigest()
        index = cache / f"{key}.txt"
        if index.is_file():
            remembered, _, variant_text = index.read_text(encoding="ascii").strip().partition("\n")
            if re.fullmatch(r"[0-9a-f]{64}", remembered) and variant_text in {"0", "1", "2"}:
                variant_index = int(variant_text)
                artifact = cache / f"{remembered}.pdf"
                if variant_index < len(variants) and artifact.is_file() and _digest(artifact) == remembered:
                    return artifact, remembered, *_changes(source.rows, variants[variant_index])

        tectonic = shutil.which("tectonic")
        if not tectonic:
            raise ResumeError("Install Tectonic to compile the LaTeX project.")
        original_layout = _layout(original, source.title, source.rows)
        original_render, old_warnings = _compile(tectonic, clone, entry, stage / "baseline")
        baseline = _layout(original_render, source.title, source.rows)
        if original_layout.pages != baseline.pages or original_layout.outside_skills != baseline.outside_skills:
            raise ResumeError("The attached LaTeX source does not match the uploaded resume.")
        if original_layout.outside_links != baseline.outside_links:
            raise ResumeError("The attached LaTeX source changes links from the uploaded resume.")

        for variant_index, (variant, modified) in enumerate(zip(variants, renderings)):
            editable.write_text(modified, encoding="utf-8")
            render, new_warnings = _compile(tectonic, clone, entry, stage / f"candidate-{variant_index}")
            try:
                layout = _layout(render, source.title, variant, expected_pages=baseline.pages)
                _check_variant(baseline, layout, old_warnings, new_warnings)
            except _SkillsLayoutError:
                if variant_index + 1 == len(variants):
                    raise
                continue
            break
        data = render.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        artifact = cache / f"{digest}.pdf"
        if not artifact.exists() or _digest(artifact) != digest:
            _atomic_write(artifact, data)
        _atomic_write(index, f"{digest}\n{variant_index}".encode("ascii"))
        return artifact, digest, *_changes(source.rows, variant)


def prepare_resume(
    resumes: Sequence[Mapping],
    description: str,
    verified_skills: str | Sequence[str],
    tailor: bool,
    cache_dir: str | Path,
) -> ResumeProposal:
    """Choose an immutable original or produce a checked skills-only variant."""
    if not resumes:
        raise ResumeError("Upload at least one resume PDF to prepare applications.")
    catalog, confirmed = _job_catalog(description, verified_skills)
    requirements = _weighted_skills(description, catalog)
    relevant_skills = tuple(skill for skill in catalog if skill.name in requirements)
    scored = []
    for resume in resumes:
        pdf_path = Path(resume["path"])
        text = resume.get("text")
        if text is None:
            text = read_pdf(pdf_path).text
        present = set(_find_skills(_normalize(text), relevant_skills))
        scored.append((sum(requirements[name] for name in present), bool(resume.get("is_default")), resume, present))
    _, _, selected, present = max(scored, key=lambda item: item[:2])
    original = Path(selected["path"]).resolve()
    try:
        digest = _digest(original)
    except OSError as error:
        raise ResumeError("The selected resume PDF is no longer available.") from error
    if selected.get("sha256") and selected["sha256"] != digest:
        raise ResumeError("The selected resume PDF changed. Upload it again before preparing applications.")

    gaps = [name for name in requirements if name not in present and (not tailor or name not in confirmed)]
    warning = ""
    if tailor and requirements:
        if selected.get("source_dir"):
            try:
                pdf, sha256, additions, removals, reordered = _tailor(
                    selected, original, digest, catalog, confirmed, requirements, Path(cache_dir), description=description,
                )
                return ResumeProposal(
                    selected["id"], selected["name"], str(pdf), sha256,
                    pdf != original, additions, removals, gaps, reordered=reordered,
                )
            except ResumeError as error:
                warning = f"{error} The original PDF is ready for review."
            except (OSError, UnicodeError):
                warning = "The LaTeX project or cache could not be read. The original PDF is ready for review."
        else:
            warning = "This resume has no LaTeX source attached. The original PDF is ready for review."

    return ResumeProposal(
        selected["id"], selected["name"], str(original), digest,
        gaps=[name for name in requirements if name not in present], warning=warning,
    )
