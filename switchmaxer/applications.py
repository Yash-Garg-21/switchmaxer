"""Submit approved applications to the local test portal and route live handoffs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fastapi import APIRouter


_TEST_SCENARIOS = {
    "success": ("Software Engineer II", "The test portal accepts the application and returns a receipt."),
    "needs_action": ("Backend Software Engineer", "The test portal waits for you to complete a simulated verification step."),
    "ambiguous": ("Platform Software Engineer", "The test portal accepts the application but the response times out."),
}


def _test_path(scenario: str) -> str:
    return f"/test-portal/jobs/{scenario}"


TEST_JOBS = tuple(
    {
        "source": "test_portal",
        "source_id": scenario,
        "company": "Local Test Portal",
        "title": title,
        "location": "Remote",
        "url": _test_path(scenario),
        "apply_url": _test_path(scenario),
        "description": f"{scenario_description}\n\nRequirements: 2–4 years of software engineering experience and Python. Docker is preferred.",
        "published_date": None,
        "requisition_id": f"test-{scenario}",
        "test_scenario": scenario,
    }
    for scenario, (title, scenario_description) in _TEST_SCENARIOS.items()
)


@dataclass(frozen=True, slots=True)
class ApprovedApplication:
    id: int
    job_id: int
    company: str
    role: str
    destination: str
    pdf_path: str | Path
    pdf_sha256: str
    answers: Mapping[str, str]
    test_scenario: str | None = None
    original_resume: str = ""
    added_skills: tuple[str, ...] = ()
    removed_skills: tuple[str, ...] = ()
    tailored: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))
        object.__setattr__(self, "added_skills", tuple(self.added_skills))
        object.__setattr__(self, "removed_skills", tuple(self.removed_skills))


ApplicationStatus = Literal["submitted", "needs_action", "needs_verification", "manual_handoff", "failed"]


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    status: ApplicationStatus
    detail: str
    receipt: str | None = None


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class TestPortal:
    """Local fixture with durable receipts and an idempotent submission endpoint."""

    __test__ = False

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS test_portal_receipts (
                    application_id INTEGER PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    receipt TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS test_portal_challenges (
                    application_id INTEGER PRIMARY KEY,
                    completed INTEGER NOT NULL DEFAULT 0
                )
            """)

    def receipt_for(self, application_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT receipt FROM test_portal_receipts WHERE application_id = ?", (application_id,)
            ).fetchone()
        return row[0] if row else None

    def complete_challenge(self, application_id: int) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE test_portal_challenges SET completed = 1 WHERE application_id = ?", (application_id,)
            )
            return result.rowcount == 1

    def submit(self, approved: ApprovedApplication, pdf: bytes) -> ApplicationOutcome:
        scenario = approved.test_scenario
        if scenario not in _TEST_SCENARIOS or approved.destination != _test_path(scenario):
            return ApplicationOutcome("failed", "The approved destination is invalid for the local test portal.")

        pdf_hash = hashlib.sha256(pdf).hexdigest()
        if b"%PDF-" not in pdf[:1024] or pdf_hash != approved.pdf_sha256:
            return ApplicationOutcome("failed", "The approved PDF changed. Prepare and approve the resume again.")

        payload = {
            "answers": dict(approved.answers),
            "job_id": approved.job_id,
            "company": approved.company,
            "role": approved.role,
            "destination": approved.destination,
            "pdf_sha256": pdf_hash,
        }
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        receipt = f"TEST-{approved.id}-{fingerprint[:10].upper()}"

        with self._connect() as db:
            if scenario == "needs_action":
                db.execute(
                    "INSERT OR IGNORE INTO test_portal_challenges (application_id) VALUES (?)", (approved.id,)
                )
                row = db.execute(
                    "SELECT completed FROM test_portal_challenges WHERE application_id = ?", (approved.id,)
                ).fetchone()
                if not row[0]:
                    return ApplicationOutcome("needs_action", "Complete the test verification step to continue.")

            inserted = db.execute(
                "INSERT OR IGNORE INTO test_portal_receipts VALUES (?, ?, ?)", (approved.id, fingerprint, receipt)
            ).rowcount
            recorded = db.execute(
                "SELECT fingerprint, receipt FROM test_portal_receipts WHERE application_id = ?", (approved.id,)
            ).fetchone()

        if recorded[0] != fingerprint:
            return ApplicationOutcome("needs_verification", "A different application already has a test receipt for this ID.")
        if scenario == "ambiguous" and inserted:
            # The portal committed the receipt before its response was lost.
            raise TimeoutError("The test portal response timed out after submission.")
        return ApplicationOutcome("submitted", "The test portal confirmed submission.", recorded[1])


def _apply(approved: ApprovedApplication, portal: TestPortal) -> ApplicationOutcome:
    try:
        pdf = Path(approved.pdf_path).read_bytes()
    except OSError:
        return ApplicationOutcome("failed", "The approved resume PDF could not be opened.")

    if approved.test_scenario is None:
        if hashlib.sha256(pdf).hexdigest() != approved.pdf_sha256:
            return ApplicationOutcome("failed", "The approved PDF changed. Prepare and approve the resume again.")
        return ApplicationOutcome("manual_handoff", "Open the live employer form and submit with the approved PDF and answers.")

    try:
        return portal.submit(approved, pdf)
    except TimeoutError:
        return ApplicationOutcome("needs_verification", "The test portal response timed out. Check for a receipt before continuing.")
    except sqlite3.Error:
        return ApplicationOutcome("needs_verification", "The test portal could not return a result. Check for a receipt before continuing.")


def run_application_batch(
    claim_next: Callable[[], ApprovedApplication | None],
    finish: Callable[[int, ApplicationOutcome], None],
    portal: TestPortal,
) -> int:
    """Drain applications claimed atomically by the store, continuing past per-job pauses."""
    processed = 0
    while (approved := claim_next()) is not None:
        finish(approved.id, _apply(approved, portal))
        processed += 1
    return processed


def create_test_portal_router(portal: TestPortal) -> APIRouter:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    router = APIRouter(prefix="/test-portal", tags=["Local test portal"])

    @router.get("/jobs/{scenario}", response_class=HTMLResponse)
    def test_job(scenario: str) -> str:
        fixture = next((job for job in TEST_JOBS if job["test_scenario"] == scenario), None)
        if not fixture:
            raise HTTPException(status_code=404, detail="Test job not found")
        title = escape(fixture["title"])
        description = escape(fixture["description"])
        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} · Local Test Portal</title>
    <style>
        body {{ font: 16px/1.6 system-ui, sans-serif; color: #212520; background: #f6f5f0;
            max-width: 600px; margin: 65px auto; padding: 0 24px; }}
        a {{ color: #24513d; }}
        p {{ white-space: pre-line; }}
    </style>
</head>
<body>
    <a href="/">Back to dashboard</a>
    <h1>{title}</h1>
    <p>Local Test Portal · Remote</p>
    <p>{description}</p>
    <p>Approve this job in the dashboard to test a submission.</p>
</body>
</html>"""

    @router.get("/receipts/{application_id}")
    def test_receipt(application_id: int) -> dict[str, str]:
        receipt = portal.receipt_for(application_id)
        if not receipt:
            raise HTTPException(status_code=404, detail="No test receipt found")
        return {"receipt": receipt}

    return router
