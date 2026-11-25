import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from switchmaxer.discovery import JobRecord, SourceResult
from switchmaxer.main import create_app
from switchmaxer.storage import Store, default_data_dir


def sample_pdf():
    # A one-page applicant fixture with searchable PDF text.
    content = b"BT /F1 12 Tf 40 700 Td (Python applicant@example.com) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, content in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(index).encode() + b" 0 obj\n" + content + b"\nendobj\n")
    start = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


class DashboardTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)

    def token(self, client, path="/"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]

    def post(self, client, path, **data):
        return client.post(path, data={"csrf": self.token(client), **data})

    def test_browser_upload_returns_to_profile_and_blocks_cross_site_posts(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            profile = client.get("/profile")
            self.assertEqual(profile.headers["Referrer-Policy"], "same-origin")
            data = {"csrf": self.token(client, "/profile")}
            files = {"resumes": ("General.pdf", sample_pdf(), "application/pdf")}
            external = client.post("/profile/resumes", data=data, files=files, headers={
                "Origin": "null", "Sec-Fetch-Site": "cross-site",
            })
            self.assertEqual(external.status_code, 403)
            self.assertEqual(app.state.store.resumes(), [])
            native = client.post("/profile/resumes", data=data, files=files, headers={
                "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin",
            })
            self.assertEqual(native.url.path, "/profile")
            self.assertIn("Added 1 resume.", native.text)
            direct = client.get("/profile/resumes", follow_redirects=False)
            self.assertEqual(direct.status_code, 303)
            self.assertEqual(direct.headers["Location"], "/profile")

    def test_batch_receipts_persist_after_restart_and_reports_update(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            self.assertIn("SwitchMaxer home", client.get("/").text)
            upload = client.post("/profile/resumes", data={"csrf": self.token(client, "/profile")}, files={
                "resumes": ("General.pdf", sample_pdf(), "application/pdf"),
            })
            self.assertIn("Added 1 resume.", upload.text)
            self.assertIn('value="applicant@example.com"', upload.text)
            self.assertIn('value="60"', upload.text)
            self.assertIn("Local Test Portal", self.post(client, "/sources/test-jobs").text)
            for job in app.state.store.jobs():
                self.post(client, f"/jobs/{job['id']}/flag", flag="shortlisted", value="1")
            review = self.post(client, "/review/prepare")
            self.assertIn("Prepared 3 applications", review.text)
            approvals = dict(re.findall(r'name="(approval_\d+)" value="([^"]+)"', review.text))
            ids = [key.removeprefix("approval_") for key in approvals]
            self.assertEqual(len(ids), 3)
            self.assertEqual(client.get(f"/review/{ids[0]}/pdf").content, sample_pdf())
            self.post(client, "/review/approve", selected=ids, **approvals)
            results = {row["status"]: row for row in app.state.store.applications()}
            self.assertEqual(set(results), {"submitted", "needs_action", "needs_verification"})
            self.assertEqual(results["submitted"]["snapshot"]["answers"]["Email"], "applicant@example.com")
            self.assertIn("1 confirmed application", client.get("/exports/applications.html").text)
            challenge_id = results["needs_action"]["id"]
            uncertain_id = results["needs_verification"]["id"]

        restarted = create_app(self.data)
        with TestClient(restarted) as client:
            self.assertEqual(len(restarted.state.store.applications()), 3)
            self.assertIn("Needs verification", client.get("/applications").text)
            self.post(client, f"/applications/{challenge_id}/resume")
            self.post(client, f"/applications/{uncertain_id}/receipt")
            results = restarted.state.store.applications()
            self.assertTrue(all(row["status"] == "submitted" for row in results))
            self.assertEqual(len({row["receipt"] for row in results}), 3)
            report = client.get("/exports/applications.html").text
            self.assertIn("3 confirmed applications", report)
            self.assertIn("Successful applications · SwitchMaxer", report)
            self.assertEqual(report.count("General.pdf"), 3)
            self.assertEqual(client.get("/exports/jobs.html").text.count("checked"), 3)

    def test_refresh_deduplicates_alert_and_preserves_checkbox(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            self.post(client, "/sources/save", name="Acme", kind="lever", url="https://jobs.lever.co/acme")
            self.post(client, "/sources/import", source="linkedin", url="https://www.linkedin.com/jobs/view/12345",
                      company="Acme", title="Software Engineer II", location="Bengaluru")
            job, = app.state.store.jobs()
            self.post(client, f"/jobs/{job['id']}/flag", flag="manually_applied", value="1")
            record = JobRecord(
                "lever", "role-1", "Acme", "Software Engineer II", "Bengaluru", "https://jobs.lever.co/acme/role-1",
                "https://jobs.lever.co/acme/role-1/apply", "2–4 years of experience. Python.",
            )
            with patch("switchmaxer.main.starter_sources") as starters, patch(
                "switchmaxer.main.refresh_sources",
                side_effect=lambda sources: [SourceResult(sources[0], [record], "success")],
            ) as refresh:
                self.post(client, "/sources/refresh")
                self.post(client, "/sources/refresh")
            starters.assert_not_called()
            self.assertEqual([[source["name"] for source in call.args[0]] for call in refresh.call_args_list],
                             [["Acme"], ["Acme"]])
            source, = app.state.store.sources()
            self.assertEqual((source["name"], source["status"]), ("Acme", "success"))
            job, = app.state.store.jobs()
            self.assertEqual(job["apply_url"], record.apply_url)
            self.assertTrue(job["manually_applied"])
            self.assertEqual(job["sources"], ["lever", "linkedin"])
            self.assertIn("Applied elsewhere", client.get("/applications").text)
            self.assertIn("checked", client.get("/exports/jobs.html").text)

    def test_first_refresh_bootstraps_once_and_honors_removal(self):
        app = create_app(self.data)
        store = app.state.store
        starter_rows = [{"name": "Starter", "kind": "lever", "url": "https://jobs.lever.co/starter",
                         "slug": "starter", "crawl_permission": False}]
        with TestClient(app) as client, patch(
            "switchmaxer.main.starter_sources", return_value=starter_rows,
        ) as starters, patch(
            "switchmaxer.main.refresh_sources", side_effect=lambda sources: [
                SourceResult(source, [], "success") for source in sources
            ],
        ) as refresh:
            store.save_profile({"locations": "Bengaluru"})
            self.assertTrue(client.get("/").context["can_add_starter_sources"])
            self.assertTrue(client.get("/sources").context["can_add_starter_sources"])
            self.assertEqual(store.sources(), [])
            starters.assert_not_called()

            response = self.post(client, "/sources/refresh")
            self.assertIn("Refreshed 1 of 1 sources", response.text)
            source, = store.sources()
            self.assertEqual((source["name"], source["status"]), ("Starter", "success"))
            self.assertFalse(response.context["can_add_starter_sources"])
            self.post(client, "/sources/refresh", return_to="sources")
            self.assertEqual([item["id"] for item in store.sources()], [source["id"]])
            starters.assert_called_once_with(store.profile())
            self.assertEqual(refresh.call_count, 2)

            self.post(client, f"/sources/{source['id']}/delete")
            self.assertFalse(client.get("/sources").context["can_add_starter_sources"])
            response = self.post(client, "/sources/refresh")
            self.assertIn("Add a company source", response.text)
            self.assertEqual(store.sources(), [])
            self.assertEqual(refresh.call_count, 2)
            self.assertEqual(starters.call_count, 1)

    def test_deleting_cli_configured_source_before_first_refresh_prevents_starters(self):
        from switchmaxer.setup import configure

        configure(self.data, {"sources": [{"name": "Configured", "kind": "lever",
                                          "url": "https://jobs.lever.co/configured"}]})
        app = create_app(self.data)
        with TestClient(app) as client:
            source, = app.state.store.sources()
            self.post(client, f"/sources/{source['id']}/delete")

        with TestClient(create_app(self.data)) as client, patch(
            "switchmaxer.main.starter_sources",
        ) as starters, patch("switchmaxer.main.refresh_sources") as refresh:
            response = self.post(client, "/sources/refresh", return_to="sources")
            self.assertIn("Add a company source", response.text)
            self.assertEqual(response.context["sources"], [])
            self.assertFalse(response.context["can_add_starter_sources"])
            starters.assert_not_called()
            refresh.assert_not_called()

    def test_sources_saves_smartrecruiters_board_with_country_filter(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            url = "https://careers.smartrecruiters.com/example?country=in"
            response = self.post(client, "/sources/save", name="Example", kind="smartrecruiters", url=url)
            self.assertIn("Company source saved.", response.text)
            self.assertIn("SmartRecruiters", response.text)
            self.assertIn("1 source · Public feeds", response.text)
            source, = app.state.store.sources()
            self.assertEqual((source["kind"], source["url"]), ("smartrecruiters", url))

    def test_old_tailoring_proposal_needs_preparation_again(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            client.post("/profile/resumes", data={"csrf": self.token(client, "/profile")}, files={
                "resumes": ("General.pdf", sample_pdf(), "application/pdf"),
            })
            store = app.state.store
            store.save_profile(store.profile() | {"tailor": True})
            self.post(client, "/sources/test-jobs")
            job = store.jobs()[0]
            self.post(client, f"/jobs/{job['id']}/flag", flag="shortlisted", value="1")
            self.assertIn(f'name="approval_{job["id"]}"', self.post(client, "/review/prepare").text)
            with store.db(write=True) as db:
                saved = db.execute("SELECT configuration FROM proposals WHERE job_id = ?", (job["id"],)).fetchone()
                old_configuration = json.loads(saved["configuration"])
                del old_configuration["tailoring_revision"]
                db.execute("UPDATE proposals SET configuration = ? WHERE job_id = ?",
                           (json.dumps(old_configuration, sort_keys=True), job["id"]))
            review = client.get("/review").text
            self.assertIn("Resume preparation needed", review)
            self.assertNotIn(f'name="approval_{job["id"]}"', review)
            self.assertIn(f'name="approval_{job["id"]}"', self.post(client, "/review/prepare").text)

    def test_reimport_merges_existing_jobs_when_employer_link_is_discovered(self):
        app = create_app(self.data)
        with TestClient(app) as client:
            self.post(client, "/sources/import", source="linkedin", url="https://www.linkedin.com/jobs/view/12345")
            self.post(client, "/sources/import", source="manual", url="https://example.com/jobs/1", title="Engineer")
            linkedin = next(job for job in app.state.store.jobs() if job["source"] == "linkedin")
            self.post(client, f"/jobs/{linkedin['id']}/flag", flag="manually_applied", value="1")
            self.post(client, "/sources/import", source="linkedin", url=linkedin["url"],
                      apply_url="https://example.com/jobs/1")
            job, = app.state.store.jobs()
            self.assertTrue(job["manually_applied"])
            self.assertEqual(job["apply_url"], "https://example.com/jobs/1")


class PromptSetupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / "data"
        self.pdf = self.data.parent / "sample.pdf"
        self.pdf.write_bytes(sample_pdf())

    def run_setup(self, command, payload=None, *, package="switchmaxer"):
        return subprocess.run(
            [sys.executable, "-m", f"{package}.setup", "--data-dir", str(self.data), command],
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True, text=True, timeout=20,
        )

    def test_inspect_is_read_only_before_onboarding(self):
        result = self.run_setup("inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "unconfigured")
        self.assertEqual(summary["defaults_in_use"]["notice_days"], "60")
        self.assertNotIn("notice_days", summary["profile_needed"])
        self.assertFalse(self.data.exists())

    def test_setup_merges_profile_and_is_safe_to_repeat(self):
        source = {"name": "Example", "kind": "greenhouse", "url": "https://job-boards.greenhouse.io/example"}
        config = {
            "resumes": [{"path": str(self.pdf), "name": "General.pdf", "default": True}],
            "profile": {"current_role": "Software Engineer II", "notice_days": "45", "fixed_ctc": "1234.50"},
            "sources": [source],
        }
        result = self.run_setup("configure", config)
        self.assertEqual(result.returncode, 0, result.stderr)
        safe_summary = json.loads(result.stdout)
        self.assertEqual(safe_summary["status"], "ready")
        self.assertNotIn("applicant@example.com", result.stdout)
        self.assertNotIn("1234.50", result.stdout)
        self.assertTrue(safe_summary["suggested_email_available"])
        store = Store(self.data)
        with store.db(write=True) as db:
            db.execute("UPDATE sources SET status = 'success'")
        result = self.run_setup("configure", config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(store.resumes()), 1)
        self.assertEqual(len(store.sources()), 1)
        self.assertEqual(store.sources()[0]["status"], "success")
        self.assertEqual(store.applications(), [])
        patch_result = self.run_setup("configure", {"profile": {"locations": "Bengaluru"}})
        self.assertEqual(patch_result.returncode, 0, patch_result.stderr)
        self.assertEqual(store.profile()["fixed_ctc"], "1234.50")
        self.assertEqual(store.profile()["locations"], "Bengaluru")
        self.assertNotIn("1234.50", self.run_setup("inspect").stdout)
        with TestClient(create_app(self.data)) as client:
            profile = client.get("/profile").text
            self.assertIn("General.pdf", profile)
            self.assertIn('value="applicant@example.com"', profile)
            self.assertIn('value="45"', profile)

    def test_bad_configuration_changes_neither_profile_nor_resumes(self):
        result = self.run_setup("configure", {"profile": {"current_role": "Engineer"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_setup("configure", {
            "resumes": [{"path": str(self.pdf)}],
            "profile": {"notice_days": "755", "current_role": "Wrong update"},
        })
        self.assertEqual(result.returncode, 2, result.stderr)
        store = Store(self.data)
        self.assertEqual(store.profile()["current_role"], "Engineer")
        self.assertEqual(store.resumes(), [])

    def test_previous_setup_and_dashboard_commands_share_data(self):
        from jobapply.main import create_app as previous_create_app
        from jobapply.storage import PROFILE_DEFAULTS as previous_profile_defaults

        self.assertIn("target_titles", previous_profile_defaults)
        configuration = {"resumes": [{"path": str(self.pdf), "name": "Previous.pdf"}]}
        previous = self.run_setup("configure", configuration, package="jobapply")
        self.assertEqual(previous.returncode, 0, previous.stderr)
        current = self.run_setup("inspect")
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(json.loads(previous.stdout), json.loads(current.stdout))
        with TestClient(previous_create_app(self.data)) as client:
            profile = client.get("/profile").text
            self.assertIn("SwitchMaxer home", profile)
            self.assertIn("Previous.pdf", profile)


class DefaultDataPathTest(unittest.TestCase):
    def test_switchmaxer_override_takes_precedence_over_previous_override(self):
        with patch.dict(os.environ, {"SWITCHMAXER_DATA_DIR": "current", "JOBAPPLY_DATA_DIR": "previous"}, clear=True):
            self.assertEqual(default_data_dir(), Path("current"))
        with patch.dict(os.environ, {"JOBAPPLY_DATA_DIR": "previous"}, clear=True):
            self.assertEqual(default_data_dir(), Path("previous"))

    def test_mac_keeps_legacy_workspace_and_uses_new_default_for_fresh_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            support = home / "Library" / "Application Support"
            with patch.dict(os.environ, {}, clear=True), patch("switchmaxer.storage.sys.platform", "darwin"), \
                    patch("switchmaxer.storage.Path.home", return_value=home):
                self.assertEqual(default_data_dir(), support / "SwitchMaxer")
                legacy = support / "JobApply"
                legacy.mkdir(parents=True)
                self.assertEqual(default_data_dir(), legacy)

    def test_linux_and_windows_use_native_data_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            custom = home / "custom"
            cases = [
                ("linux", {}, home / ".local" / "share" / "switchmaxer"),
                ("linux", {"XDG_DATA_HOME": str(custom)}, custom / "switchmaxer"),
                ("win32", {}, home / "AppData" / "Local" / "SwitchMaxer"),
                ("win32", {"LOCALAPPDATA": str(custom)}, custom / "SwitchMaxer"),
            ]
            for platform, environment, expected in cases:
                with self.subTest(platform=platform, override=bool(environment)), \
                        patch.dict(os.environ, environment, clear=True), \
                        patch("switchmaxer.storage.sys.platform", platform), \
                        patch("switchmaxer.storage.Path.home", return_value=home):
                    self.assertEqual(default_data_dir(), expected)


if __name__ == "__main__":
    unittest.main()
