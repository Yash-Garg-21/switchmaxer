import hashlib
import sqlite3
import tempfile
import unittest
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from switchmaxer.applications import ApprovedApplication, TestPortal, run_application_batch


class ApplicationWorkerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data = Path(temporary.name)
        self.database = data / "test.sqlite3"
        self.pdf = b"%PDF-1.7\ntest fixture\n%%EOF\n"
        self.pdf_path = data / "resume.pdf"
        self.pdf_path.write_bytes(self.pdf)
        self.pdf_sha256 = hashlib.sha256(self.pdf).hexdigest()
        self.portal = TestPortal(self.connect)
        self.queue = deque()
        self.outcomes = {}

    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.database)
        try:
            with database:
                yield database
        finally:
            database.close()

    def approval(self, identifier, scenario):
        return ApprovedApplication(
            id=identifier,
            job_id=identifier,
            company="Local Test Portal",
            role="Software Engineer II",
            destination=f"/test-portal/jobs/{scenario}",
            pdf_path=self.pdf_path,
            pdf_sha256=self.pdf_sha256,
            answers={"notice_days": "60"},
            test_scenario=scenario,
        )

    def drain(self):
        return run_application_batch(
            lambda: self.queue.popleft() if self.queue else None,
            self.outcomes.__setitem__,
            self.portal,
        )

    def test_batch_continues_past_pause_and_timeout(self):
        waiting = self.approval(1, "needs_action")
        ambiguous = self.approval(2, "ambiguous")
        success = self.approval(3, "success")
        self.queue.extend((waiting, ambiguous, success))

        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.outcomes[1].status, "needs_action")
        self.assertEqual(self.outcomes[2].status, "needs_verification")
        self.assertEqual(self.outcomes[3].status, "submitted")
        self.assertIsNone(self.outcomes[2].receipt)
        self.assertIsNone(self.portal.receipt_for(1))

        self.portal = TestPortal(self.connect)
        receipt = self.portal.receipt_for(2)
        self.assertIsNotNone(receipt)
        self.assertEqual(self.drain(), 0)
        self.assertTrue(self.portal.complete_challenge(1))
        self.queue.append(waiting)
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.outcomes[1].status, "submitted")
        self.assertEqual(self.portal.submit(ambiguous, self.pdf).receipt, receipt)
        with self.connect() as database:
            count = database.execute("SELECT COUNT(*) FROM test_portal_receipts").fetchone()[0]
        self.assertEqual(count, 3)

    def test_live_handoff_and_stale_approval(self):
        live = replace(self.approval(1, "success"), test_scenario=None, destination="https://example.com/jobs/1")
        mismatched = replace(self.approval(2, "success"), destination="https://example.com/jobs/2")
        changed = replace(self.approval(3, "success"), pdf_sha256="different")
        self.queue.extend((live, mismatched, changed))
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.outcomes[1].status, "manual_handoff")
        self.assertEqual(self.outcomes[2].status, "failed")
        self.assertEqual(self.outcomes[3].status, "failed")
        for application_id in range(1, 4):
            self.assertIsNone(self.portal.receipt_for(application_id))
