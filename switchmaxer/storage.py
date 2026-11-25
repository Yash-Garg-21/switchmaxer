import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .discovery import ATS, canonical_url
from .resumes import TAILORING_REVISION


PROFILE_DEFAULTS = {
    "name": "", "email": "", "phone": "", "notice_days": "60", "start_date": "",
    "fixed_ctc": "", "variable_ctc": "", "expected_ctc": "", "ctc_units": "INR LPA",
    "current_role": "", "experience_years": "", "target_titles": "", "target_levels": "",
    "locations": "", "work_modes": "", "include_companies": "", "exclude_companies": "",
    "work_authorization": "", "sponsorship": "", "relocation": "", "portfolio": "",
    "tailor": False, "verified_skills": "",
}


def default_data_dir():
    if override := os.environ.get("SWITCHMAXER_DATA_DIR") or os.environ.get("JOBAPPLY_DATA_DIR"):
        return Path(override).expanduser()

    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        legacy = support / "JobApply"
        return legacy if legacy.is_dir() else support / "SwitchMaxer"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "SwitchMaxer"

    configured = os.environ.get("XDG_DATA_HOME")
    data_home = Path(configured) if configured and Path(configured).is_absolute() else home / ".local" / "share"
    return data_home / "switchmaxer"


def validate_profile(profile):
    notice = profile.get("notice_days")
    if notice and (not re.fullmatch(r"[0-9]{1,3}", notice) or int(notice) > 730):
        raise ValueError("Enter a notice period between 0 and 730 days.")
    if start := profile.get("start_date"):
        try:
            if date.fromisoformat(start).isoformat() != start:
                raise ValueError
        except ValueError:
            raise ValueError("Enter the earliest start date as YYYY-MM-DD.") from None
    if years := profile.get("experience_years"):
        try:
            if not 0 <= float(years) <= 90:
                raise ValueError
        except ValueError:
            raise ValueError("Enter years of experience between 0 and 90.") from None
    if (email := profile.get("email")) and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid application email.")
    if (portfolio := profile.get("portfolio")) and not canonical_url(portfolio):
        raise ValueError("Enter a valid public profile URL.")


SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    text TEXT NOT NULL,
    email TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    source_dir TEXT NOT NULL DEFAULT '',
    entry_file TEXT NOT NULL DEFAULT '',
    skills_file TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_default_resume ON resumes(is_default) WHERE is_default = 1;
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    crawl_permission INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT NOT NULL DEFAULT '',
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY
);
CREATE TRIGGER IF NOT EXISTS remember_source_registry AFTER INSERT ON sources
BEGIN
    INSERT OR IGNORE INTO metadata(key) VALUES ('source_registry_initialized');
END;
INSERT OR IGNORE INTO metadata(key) SELECT 'source_registry_initialized' FROM sources LIMIT 1;
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    shortlisted INTEGER NOT NULL DEFAULT 0,
    manually_applied INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS origins (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    data TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS origins_job_id ON origins(job_id);
CREATE TABLE IF NOT EXISTS proposals (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    data TEXT NOT NULL,
    job_version TEXT NOT NULL,
    configuration TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    receipt TEXT,
    confirmed_at TEXT,
    updated_at TEXT NOT NULL
);
"""

SOURCE_KINDS = ATS | {"html"}
EMPLOYER_SOURCES = SOURCE_KINDS | {"company"}
_INSERT_SOURCE = "INSERT INTO sources(name,url,kind,slug,crawl_permission) VALUES (?,?,?,?,?)"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def job_version(job):
    return json_text({key: job.get(key) for key in (
        "company", "title", "location", "description", "url", "apply_url", "test_scenario"
    )})


def resume_configuration(profile, resumes):
    configuration = {
        "tailor": profile["tailor"],
        "verified_skills": profile["verified_skills"],
        "resumes": [{key: resume[key] for key in (
            "id", "sha256", "is_default", "source_dir", "entry_file", "skills_file"
        )} for resume in resumes],
    }
    if profile["tailor"]:
        configuration["tailoring_revision"] = TAILORING_REVISION
    return json_text(configuration)


def _normalized(text):
    return " ".join(re.findall(r"\w+", (text or "").casefold()))


def _opposite_sources(first, second):
    return (first["source"] in EMPLOYER_SOURCES) != (second["source"] in EMPLOYER_SOURCES)


def _requisition(record):
    return (record.get("requisition_id") or "").strip().casefold()


def _strong_keys(record):
    if record.get("test_scenario"):
        return set()
    keys = {("url", url) for key in ("url", "apply_url") if (url := canonical_url(record.get(key, "")))}
    company, req = _normalized(record.get("company")), _requisition(record)
    if company and req:
        keys.add(("requisition", company, req))
    return keys


def _role_key(record):
    if record.get("test_scenario"):
        return ()
    signature = tuple(_normalized(record.get(key)) for key in ("company", "title", "location"))
    return signature if all(signature) else ()


def _conflicting_requisition(first, second):
    left, right = _requisition(first), _requisition(second)
    return bool(
        left and right and left != right and first["source"] in EMPLOYER_SOURCES and second["source"] in EMPLOYER_SOURCES
    )


def _combined_job(records):
    ordered = sorted(records, key=lambda row: row["source"] not in EMPLOYER_SOURCES)
    primary = dict(ordered[0])
    for key in ("company", "title", "location", "description", "published_date", "work_mode", "requisition_id"):
        primary[key] = next((row[key] for row in ordered if row.get(key)), "")
    primary["apply_url"] = next((
        row["apply_url"] or row["url"] for row in ordered if row.get("apply_url") or row.get("url")
    ), "")
    primary["sources"] = list(dict.fromkeys(row["source"] for row in ordered))
    return primary


def _merge_jobs(connection, candidates, origins):
    """Consolidate a newly discovered alias without discarding application history."""
    placeholders = ",".join("?" for _ in candidates)
    jobs = list(connection.execute(
        f"SELECT j.*, a.id AS application_id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
        f"WHERE j.id IN ({placeholders})",
        sorted(candidates),
    ))
    tracked = [job for job in jobs if job["application_id"]]
    if len(tracked) > 1:
        return None
    winner = tracked[0] if tracked else min(jobs, key=lambda job: (job["first_seen"], job["id"]))
    keep = winner["id"]
    connection.execute(
        "UPDATE jobs SET first_seen=?,shortlisted=?,manually_applied=? WHERE id=?",
        (min(job["first_seen"] for job in jobs), int(any(job["shortlisted"] for job in jobs)),
         int(any(job["manually_applied"] for job in jobs)), keep),
    )
    for row in origins.values():
        if row["job_id"] in candidates:
            row["job_id"] = keep
    connection.execute(f"UPDATE origins SET job_id=? WHERE job_id IN ({placeholders})", (keep, *sorted(candidates)))
    connection.execute(f"DELETE FROM proposals WHERE job_id IN ({placeholders})", sorted(candidates))
    connection.execute(f"DELETE FROM jobs WHERE id IN ({placeholders}) AND id<>?", (*sorted(candidates), keep))
    return keep


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "jobapply.db"

    @contextmanager
    def db(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.db() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    def recover_queue(self):
        with self.db(write=True) as connection:
            connection.execute(
                "UPDATE applications SET status = 'needs_verification', detail = ?, updated_at = ? "
                "WHERE status = 'submitting'",
                ("The app closed during submission. Check for a receipt before taking further action.", utc_now()),
            )

    def profile(self, connection=None):
        if connection is None:
            with self.db() as connection:
                return self.profile(connection)
        row = connection.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        return PROFILE_DEFAULTS | (json.loads(row["data"]) if row else {})

    def save_profile(self, profile):
        with self.db(write=True) as connection:
            connection.execute(
                "INSERT INTO profile(id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json_text(profile),),
            )

    def resumes(self, connection=None):
        if connection is None:
            with self.db() as connection:
                return self.resumes(connection)
        return [dict(row) for row in connection.execute("SELECT * FROM resumes ORDER BY id")]

    def add_resume(self, *, name, path, sha256, text, email):
        with self.db(write=True) as connection:
            first = connection.execute("SELECT 1 FROM resumes LIMIT 1").fetchone() is None
            connection.execute(
                "INSERT INTO resumes(name,path,sha256,text,email,is_default) VALUES(?,?,?,?,?,?)",
                (name, str(path), sha256, text, email, int(first)),
            )

    def set_default(self, resume_id):
        with self.db(write=True) as connection:
            if not connection.execute("SELECT 1 FROM resumes WHERE id = ?", (resume_id,)).fetchone():
                raise ValueError("Resume not found.")
            connection.execute("UPDATE resumes SET is_default=0 WHERE is_default=1")
            connection.execute("UPDATE resumes SET is_default=1 WHERE id=?", (resume_id,))

    def set_resume_source(self, resume_id, source_dir, entry_file, skills_file):
        with self.db(write=True) as connection:
            if not connection.execute(
                "UPDATE resumes SET source_dir=?, entry_file=?, skills_file=? WHERE id=?",
                (source_dir, entry_file, skills_file, resume_id),
            ).rowcount:
                raise ValueError("Resume not found.")

    def sources(self, connection=None):
        if connection is None:
            with self.db() as connection:
                return self.sources(connection)
        return [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY name, id")]

    def can_add_starter_sources(self, connection=None):
        if connection is None:
            with self.db() as connection:
                return self.can_add_starter_sources(connection)
        row = connection.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM metadata WHERE key='source_registry_initialized') "
            "AND NOT EXISTS (SELECT 1 FROM sources)"
        ).fetchone()
        return bool(row[0])

    def sources_for_refresh(self, starter_sources):
        with self.db(write=True) as connection:
            if self.can_add_starter_sources(connection):
                starters = starter_sources(self.profile(connection))
                connection.execute("INSERT INTO metadata(key) VALUES ('source_registry_initialized')")
                connection.executemany(_INSERT_SOURCE, (
                    (source["name"], source["url"], source["kind"], source["slug"], int(source["crawl_permission"]))
                    for source in starters
                ))
            return self.sources(connection)

    def save_source(self, source_id, name, url, kind, slug, crawl_permission):
        values = (name, url, kind, slug, int(crawl_permission))
        with self.db(write=True) as connection:
            if source_id:
                if not connection.execute(
                    "UPDATE sources SET name=?,url=?,kind=?,slug=?,crawl_permission=?,"
                    "status='ready',error='',checked_at=NULL WHERE id=?", (*values, source_id),
                ).rowcount:
                    raise ValueError("Company source not found.")
            else:
                connection.execute(_INSERT_SOURCE, values)

    def delete_source(self, source_id):
        with self.db(write=True) as connection:
            connection.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def ingest(self, records, *, source_results=()):
        with self.db(write=True) as connection:
            origins = {}
            strong_index, role_index = defaultdict(set), defaultdict(set)
            for row in connection.execute("SELECT * FROM origins ORDER BY rowid"):
                key = row["source"], row["source_id"]
                data = json.loads(row["data"])
                origins[key] = dict(row) | {"data": data}
                for identity in _strong_keys(data):
                    strong_index[identity].add(key)
                if signature := _role_key(data):
                    role_index[signature].add(key)
            changed_ids = set()
            for value in records:
                record = asdict(value) if is_dataclass(value) else dict(value)
                if not record.get("source") or not record.get("source_id"):
                    raise ValueError("A job source and listing ID are required.")
                source_key = (record["source"], record["source_id"])
                old = origins.get(source_key)
                job_id = old["job_id"] if old else None
                identities, signature = _strong_keys(record), _role_key(record)
                if old:
                    for identity in _strong_keys(old["data"]):
                        strong_index[identity].discard(source_key)
                    if old_signature := _role_key(old["data"]):
                        role_index[old_signature].discard(source_key)
                peers = set().union(*(strong_index[identity] for identity in identities))
                strong = {
                    origins[key]["job_id"] for key in peers
                    if not _conflicting_requisition(record, origins[key]["data"])
                }
                if job_id and strong - {job_id}:
                    candidates = strong | {job_id}
                    merged = _merge_jobs(connection, candidates, origins)
                    if merged:
                        changed_ids.difference_update(candidates)
                        job_id = merged
                elif not job_id and len(strong) == 1:
                    job_id = next(iter(strong))
                elif not job_id and not strong and signature:
                    role_peers = [origins[key] for key in role_index[signature]]
                    weak = {row["job_id"] for row in role_peers if _opposite_sources(row["data"], record)}
                    if len(weak) == 1:
                        candidate = next(iter(weak))
                        has_employer = any(row["job_id"] == candidate and row["source"] in EMPLOYER_SOURCES
                                           for row in role_peers)
                        if record["source"] not in EMPLOYER_SOURCES or not has_employer:
                            job_id = candidate
                if job_id is None:
                    job_id = connection.execute(
                        "INSERT INTO jobs(data, first_seen) VALUES (?,?)", (json_text(record), utc_now())
                    ).lastrowid
                origins[source_key] = {"source": source_key[0], "job_id": job_id, "data": record}
                for identity in identities:
                    strong_index[identity].add(source_key)
                if signature:
                    role_index[signature].add(source_key)
                connection.execute(
                    "INSERT INTO origins(source,source_id,job_id,data) VALUES(?,?,?,?) "
                    "ON CONFLICT(source,source_id) DO UPDATE SET data=excluded.data, job_id=excluded.job_id",
                    (*source_key, job_id, json_text(record)),
                )
                changed_ids.add(job_id)
            related = {job_id: [] for job_id in changed_ids}
            for row in origins.values():
                if row["job_id"] in related:
                    related[row["job_id"]].append(row["data"])
            connection.executemany("UPDATE jobs SET data=? WHERE id=?", [
                (json_text(_combined_job(group)), job_id) for job_id, group in related.items()
            ])
            for result in source_results:
                source = result.source
                connection.execute(
                    "UPDATE sources SET status=?, error=?, checked_at=? WHERE id=? AND url=? AND kind=? AND slug=?",
                    (result.status, result.error, utc_now(), source["id"], source["url"], source["kind"], source["slug"]),
                )
        return len(changed_ids)

    def jobs(self, connection=None):
        if connection is None:
            with self.db() as connection:
                return self.jobs(connection)
        rows = connection.execute(
            "SELECT j.*, a.id AS application_id, a.status AS application_status, p.data AS proposal, "
            "p.job_version AS proposal_version, p.configuration AS proposal_configuration "
            "FROM jobs j LEFT JOIN applications a ON a.job_id=j.id LEFT JOIN proposals p ON p.job_id=j.id"
        )
        jobs = [json.loads(row["data"]) | {
            "id": row["id"], "first_seen": row["first_seen"], "shortlisted": bool(row["shortlisted"]),
            "manually_applied": bool(row["manually_applied"]), "application_id": row["application_id"],
            "application_status": row["application_status"], "proposal": json.loads(row["proposal"] or "null"),
            "proposal_version": row["proposal_version"], "proposal_configuration": row["proposal_configuration"],
        } for row in rows]
        return sorted(jobs, key=lambda job: (
            job.get("published_date") or job["first_seen"][:10], job["first_seen"], job["id"],
        ), reverse=True)

    def set_job_flag(self, job_id, flag, value):
        if flag not in {"shortlisted", "manually_applied"}:
            raise ValueError("Invalid job flag.")
        with self.db(write=True) as connection:
            if flag == "manually_applied" and connection.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND status NOT IN ('failed','skipped')", (job_id,),
            ).fetchone():
                raise ValueError("Update this application from the Applications page.")
            if not connection.execute(f"UPDATE jobs SET {flag}=? WHERE id=?", (int(value), job_id)).rowcount:
                raise ValueError("Job not found.")

    def save_proposals(self, rows):
        with self.db(write=True) as connection:
            connection.executemany(
                "INSERT INTO proposals(job_id,data,job_version,configuration,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET data=excluded.data, job_version=excluded.job_version, "
                "configuration=excluded.configuration, created_at=excluded.created_at",
                [(job_id, json_text(proposal), version, configuration, utc_now())
                 for job_id, proposal, version, configuration in rows],
            )

    def approve(self, reviewed, answers_for, token_for, *, resolve_profile=None):
        with self.db(write=True) as connection:
            profile = self.profile(connection)
            resumes = self.resumes(connection)
            if resolve_profile:
                profile = resolve_profile(profile, resumes)
            configuration = resume_configuration(profile, resumes)
            jobs = {job["id"]: job for job in self.jobs(connection)}
            snapshots = []
            for job_id, token in reviewed.items():
                job = jobs.get(job_id)
                if (
                    not job or not job["shortlisted"] or job["manually_applied"] or job["application_id"]
                    or not job["proposal"] or job["proposal_configuration"] != configuration
                    or job["proposal_version"] != job_version(job) or token != token_for(job, profile)
                ):
                    raise ValueError("The batch changed since review. Review the updated jobs before approving.")
                if not job.get("apply_url"):
                    raise ValueError("An approved job needs an application link.")
                proposal = job["proposal"]
                snapshots.append((job_id, {
                    "company": job["company"], "role": job["title"], "destination": job["apply_url"],
                    "pdf_path": proposal["pdf_path"], "pdf_sha256": proposal["sha256"],
                    "original_resume": proposal["resume_name"], "added_skills": proposal["additions"],
                    "removed_skills": proposal["removals"], "answers": answers_for(profile),
                    "reordered_skills": proposal.get("reordered", []),
                    "test_scenario": job.get("test_scenario"), "tailored": proposal["tailored"],
                }))
            if not snapshots:
                raise ValueError("Select at least one prepared job.")
            now = utc_now()
            batch_id = connection.execute("INSERT INTO batches(created_at) VALUES(?)", (now,)).lastrowid
            connection.executemany(
                "INSERT INTO applications(job_id,batch_id,snapshot,status,updated_at) VALUES(?,?,?,'queued',?)",
                [(job_id, batch_id, json_text(snapshot), now) for job_id, snapshot in snapshots],
            )
        return batch_id

    def applications(self):
        with self.db() as connection:
            return [
                dict(row) | {"snapshot": json.loads(row["snapshot"])}
                for row in connection.execute("SELECT * FROM applications ORDER BY id DESC")
            ]

    def claim_next(self):
        with self.db(write=True) as connection:
            row = connection.execute(
                "SELECT id, job_id, snapshot FROM applications WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE applications SET status='submitting', updated_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
            return row["id"], row["job_id"], json.loads(row["snapshot"])

    def finish(self, application_id, outcome):
        now = utc_now()
        with self.db(write=True) as connection:
            connection.execute(
                "UPDATE applications SET status=?,detail=?,receipt=?,confirmed_at=?,updated_at=? "
                "WHERE id=? AND status='submitting'",
                (outcome.status, outcome.detail, outcome.receipt, now if outcome.status == "submitted" else None,
                 now, application_id),
            )

    def change_application(self, application_id, action, receipt=None):
        transitions = {
            "confirm": (("manual_handoff", "needs_verification", "needs_action"), "submitted", "Confirmed by you."),
            "unconfirmed": (("needs_verification",), "skipped", "You recorded that this application was not sent."),
            "skip": (("manual_handoff", "needs_action", "failed"), "skipped", "Skipped by you."),
            "resume": (("needs_action",), "queued", ""),
        }
        previous, status, detail = transitions[action]
        now = utc_now()
        with self.db(write=True) as connection:
            row = connection.execute("SELECT status FROM applications WHERE id=?", (application_id,)).fetchone()
            if not row or row["status"] not in previous:
                raise ValueError("This action is unavailable for the current application status.")
            connection.execute(
                "UPDATE applications SET status=?,detail=?,updated_at=?,confirmed_at=?,receipt=? WHERE id=?",
                (status, "Portal receipt verified." if receipt else detail, now,
                 now if status == "submitted" else None, receipt, application_id),
            )
