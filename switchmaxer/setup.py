"""Inspect and configure a local SwitchMaxer workspace from standard input."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .discovery import _feed_endpoint, canonical_url
from .resumes import ResumeError, read_pdf, validate_source
from .storage import PROFILE_DEFAULTS, Store, default_data_dir, json_text, validate_profile


_PDF_LIMIT = 15 * 1024 * 1024
_JSON_LIMIT = 1024 * 1024
_SOURCE_KINDS = {"greenhouse", "lever", "ashby", "html"}
_NUMBER_FIELDS = {"notice_days", "experience_years", "fixed_ctc", "variable_ctc", "expected_ctc"}


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class _Resume:
    name: str
    data: bytes
    digest: str
    text: str
    email: str
    default: bool
    latex: tuple[str, str, str] | None


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) - keys:
        raise SetupError(f"{label} must be an object with supported fields.")
    return value


def _items(value, label):
    if not isinstance(value, list):
        raise SetupError(f"{label} must be a list.")
    return value


def _text(value, label, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise SetupError(f"{label} must be valid text up to {limit} characters.")
    value = value.strip()
    if required and not value:
        raise SetupError(f"{label} is required.")
    return value


def _boolean(value, label):
    if not isinstance(value, bool):
        raise SetupError(f"{label} must be true or false.")
    return value


def _profile(value):
    _object(value, PROFILE_DEFAULTS.keys(), "Profile")
    patch = {}
    for key, entry in value.items():
        label = key.replace("_", " ").capitalize()
        if key == "tailor":
            patch[key] = _boolean(entry, label)
            continue
        if key in _NUMBER_FIELDS and type(entry) in {int, float}:
            if isinstance(entry, float) and not math.isfinite(entry):
                raise SetupError(f"{label} must be finite.")
            entry = str(entry)
        patch[key] = _text(entry, label, 6000)
    try:
        validate_profile(patch)
    except ValueError as error:
        raise SetupError(str(error)) from None
    return patch


def _resume(value, position):
    label = f"Resume {position}"
    _object(value, {"path", "name", "default", "latex"}, label)
    path = Path(_text(value.get("path"), f"{label} path", 4096, required=True)).expanduser()
    name = _text(value.get("name", path.name), f"{label} name", 160, required=True)
    if any(char in name for char in "/\\") or any(ord(char) < 32 for char in name) or name in {".", ".."}:
        raise SetupError(f"{label} name must be a display name without path separators.")
    default = _boolean(value.get("default", False), f"{label} default")
    if path.suffix.lower() != ".pdf":
        raise SetupError(f"{label} must be a local PDF file.")
    try:
        if not path.is_file():
            raise SetupError(f"{label} PDF could not be opened.")
        with path.open("rb") as source:
            data = source.read(_PDF_LIMIT + 1)
    except OSError:
        raise SetupError(f"{label} PDF could not be opened.") from None
    if len(data) > _PDF_LIMIT or not data.startswith(b"%PDF-"):
        raise SetupError(f"{label} must be a PDF up to 15 MB.")
    try:
        with io.BytesIO(data) as uploaded:
            info = read_pdf(uploaded)
    except ResumeError:
        raise SetupError(f"{label} PDF could not be read. Check that it opens and is unlocked.") from None

    latex = None
    if "latex" in value:
        source = _object(value["latex"], {"directory", "entry_file", "skills_file"}, f"{label} LaTeX")
        folder = _text(source.get("directory"), f"{label} LaTeX directory", 4096, required=True)
        entry = _text(source.get("entry_file", "resume.tex"), f"{label} LaTeX entry file", 400, required=True)
        skills = _text(
            source.get("skills_file", "content/skills.tex"), f"{label} LaTeX skills file", 400, required=True,
        )
        try:
            directory = str(Path(folder).expanduser().resolve())
            validate_source(directory, entry, skills)
        except (ResumeError, OSError, RuntimeError):
            raise SetupError(f"{label} LaTeX project or skills section is invalid.") from None
        latex = (directory, entry, skills)
    return _Resume(name, data, hashlib.sha256(data).hexdigest(), info.text, info.email, default, latex)


def _source(value, position):
    label = f"Source {position}"
    _object(value, {"name", "url", "kind", "slug", "crawl_permission"}, label)
    name = _text(value.get("name"), f"{label} name", 120, required=True)
    url = _text(value.get("url"), f"{label} URL", 2000, required=True)
    kind = _text(value.get("kind"), f"{label} kind", 40, required=True).casefold()
    if not canonical_url(url) or kind not in _SOURCE_KINDS:
        raise SetupError(f"{label} needs a valid careers URL and a supported source kind.")
    result = {"name": name, "url": url, "kind": kind}
    if "slug" in value:
        result["slug"] = _text(value["slug"], f"{label} board slug", 120)
    if "crawl_permission" in value:
        result["crawl_permission"] = int(_boolean(value["crawl_permission"], f"{label} crawl permission"))
    return result


def _identity(source):
    kind = source["kind"]
    if kind == "html":
        return kind, canonical_url(source["url"])
    try:
        return kind, _feed_endpoint(kind, source)
    except ValueError:
        return None


def _source_plans(incoming, existing):
    current = list(existing)
    plans = []
    for index, supplied in enumerate(incoming, 1):
        identity = _identity(supplied)
        matches = [row for row in current if identity and _identity(row) == identity]
        if not matches and "slug" not in supplied:
            matches = [row for row in current if row["kind"] == supplied["kind"]
                       and canonical_url(row["url"]) == canonical_url(supplied["url"])]
            if len(matches) > 1:
                raise SetupError(f"Source {index} matches multiple boards. Specify the board slug.")
        previous = matches[0] if matches else {}
        merged = {"slug": "", "crawl_permission": 0, **previous, **supplied}
        if not _identity(merged):
            raise SetupError(f"Source {index} needs a hosted board URL or a valid board slug.")
        if previous:
            current[current.index(previous)] = merged
        else:
            merged["id"] = -index
            current.append(merged)
        plans.append(merged)
    return plans


def _database(directory):
    return Path(directory).expanduser().resolve() / "jobapply.db"


def _readonly(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    return closing(connection)


def _sources(connection):
    return [dict(row) for row in connection.execute(
        "SELECT id, name, url, kind, slug, crawl_permission FROM sources ORDER BY id"
    )]


def _summary(resumes, profile, sources, *, initialized):
    suggested = any(row["is_default"] and row["has_email"] for row in resumes)
    defaults_in_use = {key: PROFILE_DEFAULTS[key] for key in ("notice_days", "ctc_units") if key not in profile}
    profile_set = [key for key in PROFILE_DEFAULTS if profile.get(key) or key == "tailor" and key in profile]
    return {
        "status": "ready" if resumes else "needs_resume" if initialized else "unconfigured",
        "resumes": [{
            "id": row["id"], "name": row["name"], "is_default": bool(row["is_default"]),
            "has_source": bool(row["has_source"]),
        } for row in resumes],
        "profile_set": profile_set,
        "defaults_in_use": defaults_in_use,
        "profile_needed": [key for key in PROFILE_DEFAULTS if key not in profile_set and key not in defaults_in_use],
        "suggested_email_available": bool(suggested),
        "sources": [{
            "name": row["name"], "kind": row["kind"], "url": _summary_url(row["url"]),
        } for row in sources],
    }


def _summary_url(value):
    # Existing URL query strings may contain user-specific parameters.
    safe = canonical_url(value)
    return urlsplit(safe)._replace(query="", fragment="").geturl() if safe else ""


def inspect(directory):
    path = _database(directory)
    if not path.is_file():
        return _summary([], {}, [], initialized=False)
    with _readonly(path) as connection:
        connection.execute("BEGIN")
        resumes = connection.execute(
            "SELECT id,name,is_default,source_dir != '' AS has_source,email != '' AS has_email "
            "FROM resumes ORDER BY id"
        ).fetchall()
        row = connection.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        return _summary(resumes, json.loads(row["data"]) if row else {}, _sources(connection), initialized=True)


def _write_upload(target, data):
    if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        scratch = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(scratch, target)
        finally:
            scratch.unlink(missing_ok=True)


def _save_resumes(connection, directory, resumes):
    selected = None
    for resume in resumes:
        path = directory / "resumes" / f"{resume.digest}.pdf"
        _write_upload(path, resume.data)
        existing = connection.execute(
            "SELECT id,path FROM resumes WHERE sha256=? AND name=? ORDER BY is_default DESC,id LIMIT 1",
            (resume.digest, resume.name),
        ).fetchone()
        if existing:
            resume_id = existing["id"]
            if existing["path"] != str(path):
                connection.execute("UPDATE resumes SET path=? WHERE id=?", (str(path), resume_id))
        else:
            has_any = connection.execute("SELECT 1 FROM resumes LIMIT 1").fetchone()
            resume_id = connection.execute(
                "INSERT INTO resumes(name,path,sha256,text,email,is_default) VALUES (?,?,?,?,?,?)",
                (resume.name, str(path), resume.digest, resume.text, resume.email, int(not has_any)),
            ).lastrowid
        if resume.latex is not None:
            connection.execute(
                "UPDATE resumes SET source_dir=?,entry_file=?,skills_file=? WHERE id=?", (*resume.latex, resume_id),
            )
        if resume.default:
            selected = resume_id
    if selected is not None:
        connection.execute("UPDATE resumes SET is_default=0 WHERE is_default=1 AND id != ?", (selected,))
        connection.execute("UPDATE resumes SET is_default=1 WHERE id=? AND is_default=0", (selected,))


def _save_sources(connection, plans):
    added = {}
    for plan in plans:
        values = tuple(plan[field] for field in ("name", "url", "kind", "slug", "crawl_permission"))
        previous_id = plan["id"]
        source_id = previous_id if previous_id > 0 else added.get(previous_id)
        if source_id is not None:
            connection.execute(
                "UPDATE sources SET name=?,url=?,kind=?,slug=?,crawl_permission=? WHERE id=?",
                (*values, source_id),
            )
        else:
            added[previous_id] = connection.execute(
                "INSERT INTO sources(name,url,kind,slug,crawl_permission) VALUES (?,?,?,?,?)", values,
            ).lastrowid


def configure(directory, configuration):
    _object(configuration, {"resumes", "profile", "sources"}, "Configuration")
    patches = _items(configuration.get("resumes", []), "Resumes")
    if len(patches) > 15:
        raise SetupError("Configure at most 15 resumes per command.")
    resumes = [_resume(row, index) for index, row in enumerate(patches, 1)]
    if len({(row.digest, row.name) for row in resumes if row.default}) > 1:
        raise SetupError("Select at most one default resume.")
    profile = _profile(configuration.get("profile", {}))
    sources = [_source(row, index) for index, row in enumerate(_items(configuration.get("sources", []), "Sources"), 1)]
    if not (resumes or profile or sources):
        return inspect(directory)

    path = _database(directory)
    if sources:
        if path.is_file():
            with _readonly(path) as connection:
                _source_plans(sources, _sources(connection))
        else:
            _source_plans(sources, [])

    store = Store(path.parent)
    store.initialize()
    with store.db(write=True) as connection:
        plans = _source_plans(sources, _sources(connection)) if sources else []
        _save_resumes(connection, store.directory, resumes)
        if profile:
            row = connection.execute("SELECT data FROM profile WHERE id=1").fetchone()
            merged = (json.loads(row["data"]) if row else {}) | profile
            connection.execute(
                "INSERT INTO profile(id,data) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json_text(merged),),
            )
        _save_sources(connection, plans)
    return inspect(directory)


def main():
    parser = argparse.ArgumentParser(prog="python -m switchmaxer.setup", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("command", choices=("inspect", "configure"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "configure":
            raw = sys.stdin.buffer.read(_JSON_LIMIT + 1)
            if len(raw) > _JSON_LIMIT:
                raise SetupError("Configuration JSON must be under 1 MB.")
            try:
                configuration = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                raise SetupError("Supply one valid JSON configuration object on standard input.") from None
            result = configure(arguments.data_dir, configuration)
        else:
            result = inspect(arguments.data_dir)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except SetupError as error:
        print(f"SwitchMaxer setup: {error}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        print("SwitchMaxer setup: the local workspace could not be accessed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
