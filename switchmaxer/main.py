import argparse
import asyncio
import hashlib
import hmac
import io
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .applications import ApprovedApplication, TEST_JOBS, TestPortal, create_test_portal_router, run_application_batch
from .discovery import canonical_url, experience_range, matches_profile, parse_import, refresh_sources
from .resumes import ResumeError, prepare_resume, read_pdf, suggest_skills, validate_source
from .storage import (
    PROFILE_DEFAULTS, Store, default_data_dir, job_version, json_text, resume_configuration, validate_profile,
)


PACKAGE = Path(__file__).resolve().parent
PDF_LIMIT = 15 * 1024 * 1024
ALERT_LIMIT = 2 * 1024 * 1024
JOBS_PER_PAGE = 60
DESCRIPTION_PREVIEW = 3200
SOURCES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "html": "Company site",
    "linkedin": "LinkedIn", "naukri": "Naukri", "instahyre": "Instahyre", "manual": "Manual import",
    "test_portal": "Test portal",
}
STATUSES = {
    "queued": "Queued", "submitting": "Submitting", "submitted": "Confirmed", "needs_action": "Action needed",
    "needs_verification": "Needs verification", "manual_handoff": "Ready to apply", "failed": "Failed",
    "skipped": "Skipped", "manually_applied": "Applied elsewhere",
}


def form_text(form, key, limit=6000):
    value = form.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{key.replace('_', ' ').capitalize()} is too long.")
    return value.strip()


def preview_url(job):
    link = job.get("apply_url") or job.get("url") or ""
    if job.get("test_scenario") and link.startswith("/test-portal/jobs/") and not link.startswith("//"):
        return link
    return link if canonical_url(link) else ""


def experience_label(description):
    low, high = experience_range(description or "")
    if low is None:
        return f"Up to {high:g} years" if high is not None else ""
    if high is None:
        return f"{low:g}+ years"
    if low == high:
        return f"{low:g} years"
    return f"{low:g}–{high:g} years"


@lru_cache(maxsize=3000)
def job_details(description, verified_skills):
    preview = description[:DESCRIPTION_PREVIEW]
    return {
        "description": preview, "description_truncated": len(preview) < len(description),
        "experience_label": experience_label(description), "skills": suggest_skills(preview, verified_skills)[:12],
    }


def application_answers(profile):
    values = {
        "Full name": profile["name"], "Email": profile["email"], "Phone": profile["phone"],
        "Current role": profile["current_role"], "Years of experience": profile["experience_years"],
        "Notice period": f'{profile["notice_days"]} days' if profile["notice_days"] and not profile["start_date"] else "",
        "Earliest start date": profile["start_date"],
        "Current fixed CTC": " ".join(filter(None, (profile["fixed_ctc"], profile["ctc_units"]))) if profile["fixed_ctc"] else "",
        "Current variable CTC": " ".join(filter(None, (profile["variable_ctc"], profile["ctc_units"]))) if profile["variable_ctc"] else "",
        "Expected CTC": " ".join(filter(None, (profile["expected_ctc"], profile["ctc_units"]))) if profile["expected_ctc"] else "",
        "Work authorization": profile["work_authorization"], "Sponsorship": profile["sponsorship"],
        "Relocation": profile["relocation"], "Portfolio or public profile": profile["portfolio"],
    }
    return {key: value for key, value in values.items() if value}


def effective_profile(profile, resumes):
    default = next((resume for resume in resumes if resume["is_default"]), None)
    if default and default["email"] and not profile["email"]:
        return profile | {"email": default["email"]}
    return profile


def email_warning(profile, resume):
    configured = profile.get("email", "")
    detected = resume.get("email", "")
    if configured and detected and configured.casefold() != detected.casefold():
        return f"The PDF lists {detected}. The application email is {configured}."
    return ""


def atomic_write(destination, data):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
        temp_path.replace(destination)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def create_app(data_dir=None):
    store = Store(data_dir or default_data_dir())
    csrf_token = secrets.token_urlsafe(32)
    queue_lock = Lock()
    export_lock = Lock()
    refresh_lock = Lock()
    prepare_lock = Lock()
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.globals.update(
        csrf_token=csrf_token, source_label=lambda value: SOURCES.get(value, value),
        status_label=lambda value: STATUSES.get(value, value), preview_url=preview_url,
    )
    portal = TestPortal(store.db)

    def run_queue():
        def claim():
            row = store.claim_next()
            if row:
                application_id, job_id, snapshot = row
                snapshot = {key: value for key, value in snapshot.items()
                            if key != "reordered_skills"}
                return ApprovedApplication(id=application_id, job_id=job_id, **snapshot)
            return None

        with queue_lock:
            return run_application_batch(claim, store.finish, portal)

    def visible_jobs(jobs, profile, *, text="", view="matches"):
        rows = []
        for job in jobs:
            if view == "matches" and not job.get("test_scenario") and not matches_profile(job, profile):
                continue
            if view == "shortlisted" and not job["shortlisted"]:
                continue
            if text and text not in " ".join(str(job.get(key) or "").lower() for key in (
                "company", "title", "location", "description",
            )):
                continue
            rows.append(job)
        return rows

    def decorate_jobs(jobs, profile):
        return [job | job_details(job.get("description") or "", profile["verified_skills"]) for job in jobs]

    def export_reports():
        with export_lock:
            profile = store.profile()
            applications = store.applications()
            directory = store.directory / "exports"
            data = {
                "jobs.html": ("jobs", {"jobs": decorate_jobs(store.jobs(), profile)}),
                "applications.html": ("applications", {"applications": [
                    row for row in applications if row["status"] == "submitted"
                ]}),
            }
            for filename, (kind, context) in data.items():
                html = templates.env.get_template("report.html").render(kind=kind, **context)
                atomic_write(directory / filename, html.encode())

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.initialize)
        app.state.portal = portal
        await asyncio.to_thread(store.recover_queue)
        await asyncio.to_thread(run_queue)
        await asyncio.to_thread(export_reports)
        yield

    app = FastAPI(title="SwitchMaxer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.include_router(create_test_portal_router(portal))

    @app.middleware("http")
    async def local_browser(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return PlainTextResponse("This app accepts changes from its local dashboard.", status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def render(request, page, **context):
        return templates.TemplateResponse(request=request, name=f"{page}.html", context={
            "page": page, "message": request.query_params.get("message", ""),
            "label": request.query_params.get("level", ""), **context,
        })

    def redirect(path, message="", *, error=False):
        if message:
            path += "?" + urlencode({"message": message, "level": "error" if error else "success"})
        return RedirectResponse(path, status_code=303)

    def check_form(form):
        token = form.get("csrf", "")
        if not isinstance(token, str) or not hmac.compare_digest(token, csrf_token):
            raise HTTPException(403, "Reload the dashboard before submitting this form.")

    def approval_token(job, profile):
        content = json_text({
            "id": job["id"], "job": job_version(job), "proposal": job["proposal"], "answers": application_answers(profile),
        })
        return hmac.new(csrf_token.encode(), content.encode(), hashlib.sha256).hexdigest()

    @app.get("/")
    def jobs_page(request: Request, q: str = "", view: str = "matches", p: int = 1):
        if view not in {"matches", "all", "shortlisted"}:
            view = "matches"
        profile = store.profile()
        all_jobs = store.jobs()
        matching = visible_jobs(all_jobs, profile, text=q[:120].strip().lower(), view=view)
        last_page = max(1, (len(matching) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE)
        p = min(max(1, p), last_page)
        start = (p - 1) * JOBS_PER_PAGE
        jobs = decorate_jobs(matching[start:start + JOBS_PER_PAGE], profile)
        return render(
            request, "jobs", jobs=jobs, q=q[:120], view=view, has_resumes=bool(store.resumes()),
            result_count=len(matching), p=p, last_page=last_page,
            total=len(all_jobs), shortlisted=sum(job["shortlisted"] for job in all_jobs),
            applied=sum(job["manually_applied"] or job["application_status"] == "submitted" for job in all_jobs),
            sources_count=len(store.sources()), page_href=lambda page: "/?" + urlencode({"q": q[:120], "view": view, "p": page}),
        )

    @app.post("/jobs/{job_id}/flag")
    async def job_flag(job_id: int, request: Request):
        form = await request.form()
        check_form(form)
        try:
            flag = form_text(form, "flag", 30)
            store.set_job_flag(job_id, flag, form.get("value") == "1")
            if flag == "manually_applied":
                await asyncio.to_thread(export_reports)
            view = form.get("view")
            query = {key: form[key] for key in ("q",) if form.get(key)}
            if view in {"all", "shortlisted"}:
                query["view"] = view
            if str(form.get("p", "")).isdigit():
                query["p"] = form["p"]
            return redirect("/" + ("?" + urlencode(query) if query else ""))
        except ValueError as exc:
            return redirect("/", str(exc), error=True)

    @app.get("/profile")
    def profile_page(request: Request):
        resumes = store.resumes()
        profile = effective_profile(store.profile(), resumes)
        default = next((resume for resume in resumes if resume["is_default"]), None)
        suggestions = suggest_skills(
            "\n".join(resume["text"] for resume in resumes), profile["verified_skills"], unconfirmed_only=True,
        )
        return render(request, "profile", profile=profile, resumes=resumes, default=default, suggestions=suggestions)

    @app.post("/profile/resumes")
    async def upload_resumes(request: Request):
        async with request.form(max_files=15) as form:
            check_form(form)
            uploads = [item for item in form.getlist("resumes") if isinstance(item, UploadFile) and item.filename]
            if not uploads:
                return redirect("/profile", "Choose at least one PDF.", error=True)
            added, errors = 0, []
            for upload in uploads:
                name = Path(upload.filename.replace("\\", "/")).name[:160]
                if not name.lower().endswith(".pdf"):
                    errors.append(f"{name} must be a PDF.")
                    continue
                data = await upload.read(PDF_LIMIT + 1)
                if len(data) > PDF_LIMIT or not data.startswith(b"%PDF-"):
                    errors.append(f"{name} must be a PDF under 15 MB.")
                    continue
                digest = hashlib.sha256(data).hexdigest()
                target = store.directory / "resumes" / f"{digest}.pdf"
                try:
                    with io.BytesIO(data) as uploaded:
                        info = await asyncio.to_thread(read_pdf, uploaded)
                    await asyncio.to_thread(atomic_write, target, data)
                    store.add_resume(name=name, path=target, sha256=digest, text=info.text, email=info.email or "")
                    added += 1
                except (ResumeError, OSError):
                    errors.append(f"{name} could not be opened.")
        message = " ".join([f"Added {added} resume{'s' if added != 1 else ''}.", *errors])
        return redirect("/profile", message, error=bool(errors))

    @app.post("/profile/resumes/{resume_id}/default")
    async def default_resume(resume_id: int, request: Request):
        form = await request.form()
        check_form(form)
        try:
            store.set_default(resume_id)
            return redirect("/profile", "Default resume updated.")
        except ValueError as exc:
            return redirect("/profile", str(exc), error=True)

    @app.post("/profile/resumes/{resume_id}/source")
    async def resume_source(resume_id: int, request: Request):
        form = await request.form()
        check_form(form)
        try:
            directory = form_text(form, "source_dir", 4096)
            entry = form_text(form, "entry_file", 400)
            skills = form_text(form, "skills_file", 400)
            if directory:
                directory = str(Path(directory).expanduser().resolve())
                await asyncio.to_thread(validate_source, directory, entry, skills)
            store.set_resume_source(resume_id, directory, entry if directory else "", skills if directory else "")
            return redirect("/profile", "LaTeX source updated.")
        except (ResumeError, ValueError, OSError) as exc:
            return redirect("/profile", str(exc), error=True)

    @app.post("/profile")
    async def save_profile(request: Request):
        form = await request.form()
        check_form(form)
        try:
            profile = {key: form_text(form, key) for key in PROFILE_DEFAULTS if key != "tailor"}
            profile["tailor"] = "tailor" in form
            confirmed = [value for value in form.getlist("confirmed_skills") if isinstance(value, str)]
            if confirmed:
                candidates = suggest_skills(
                    "\n".join(row["text"] for row in store.resumes()), profile["verified_skills"], unconfirmed_only=True,
                )
                profile["verified_skills"] = "\n".join(filter(None, (
                    profile["verified_skills"], *[skill for skill in candidates if skill in confirmed],
                )))
            validate_profile(profile)
            store.save_profile(profile)
            await asyncio.to_thread(export_reports)
            return redirect("/profile", "Profile saved.")
        except ValueError as exc:
            return redirect("/profile", str(exc), error=True)

    @app.get("/sources")
    def sources_page(request: Request):
        return render(request, "sources", sources=store.sources())

    @app.post("/sources/save")
    async def source_save(request: Request):
        form = await request.form()
        check_form(form)
        try:
            source_id = int(form.get("id") or "0")
            name = form_text(form, "name", 120)
            url = form_text(form, "url", 2000)
            kind = form_text(form, "kind", 40)
            slug = form_text(form, "slug", 120)
            if not name or not canonical_url(url) or kind not in {"greenhouse", "lever", "ashby", "html"}:
                raise ValueError("Enter a company name, a valid careers URL, and a source type.")
            store.save_source(source_id, name, url, kind, slug, "crawl_permission" in form)
            return redirect("/sources", "Company source saved.")
        except ValueError as exc:
            return redirect("/sources", str(exc), error=True)

    @app.post("/sources/{source_id}/delete")
    async def source_delete(source_id: int, request: Request):
        form = await request.form()
        check_form(form)
        store.delete_source(source_id)
        return redirect("/sources", "Source removed. Previously discovered jobs are still in your workspace.")

    @app.post("/sources/refresh")
    async def source_refresh(request: Request):
        form = await request.form()
        check_form(form)
        path = "/sources" if form.get("return_to") == "sources" else "/"
        if not refresh_lock.acquire(blocking=False):
            return redirect(path, "A refresh is already running.")
        try:
            sources = store.sources()
            if not sources:
                return redirect("/sources", "Add a company source to refresh its listings.")
            results = await asyncio.to_thread(refresh_sources, sources)
            count = store.ingest((record for result in results for record in result.records), source_results=results)
            await asyncio.to_thread(export_reports)
            completed = sum(result.status == "success" for result in results)
            return redirect(path, f"Refreshed {completed} of {len(sources)} sources. Updated {count} job listings.")
        finally:
            refresh_lock.release()

    @app.post("/sources/import")
    async def source_import(request: Request):
        async with request.form(max_files=1, max_part_size=ALERT_LIMIT) as form:
            check_form(form)
            try:
                source = form_text(form, "source", 30)
                content = form_text(form, "content", ALERT_LIMIT)
                upload = form.get("alert")
                if isinstance(upload, UploadFile) and upload.filename:
                    content = await upload.read(ALERT_LIMIT + 1)
                    if len(content) > ALERT_LIMIT:
                        raise ValueError("Upload an alert file under 2 MB.")
                fields = {key: form_text(form, key, 30000 if key == "description" else 2000) for key in (
                    "url", "company", "title", "location", "description", "apply_url", "published_date", "requisition_id"
                )}
                records = parse_import(source, content, **fields)
                if not records:
                    raise ValueError("No job listings were found. Paste a posting URL or add the job details.")
                count = store.ingest(records)
                await asyncio.to_thread(export_reports)
                return redirect("/", f"Imported {count} job listing{'s' if count != 1 else ''}.")
            except ValueError as exc:
                return redirect("/sources", str(exc), error=True)

    @app.post("/sources/test-jobs")
    async def test_jobs(request: Request):
        form = await request.form()
        check_form(form)
        store.ingest(TEST_JOBS)
        await asyncio.to_thread(export_reports)
        return redirect("/", "Loaded three test jobs. You can shortlist them without contacting an employer.")

    def review_items(profile, resumes):
        configuration = resume_configuration(profile, resumes)
        items = []
        for job in store.jobs():
            if not job["shortlisted"] or job["application_id"] or job["manually_applied"]:
                continue
            job["ready"] = bool(
                job["proposal"] and job["proposal_version"] == job_version(job)
                and job["proposal_configuration"] == configuration
            )
            if job["ready"]:
                job["approval_token"] = approval_token(job, profile)
                resume = next((row for row in resumes if row["id"] == job["proposal"]["resume_id"]), {})
                job["email_warning"] = email_warning(profile, resume)
            items.append(job)
        return items

    @app.get("/review")
    def review_page(request: Request):
        resumes = store.resumes()
        profile = effective_profile(store.profile(), resumes)
        return render(request, "review", jobs=review_items(profile, resumes), answers=application_answers(profile))

    @app.post("/review/prepare")
    async def prepare(request: Request):
        form = await request.form()
        check_form(form)
        if not prepare_lock.acquire(blocking=False):
            return redirect("/review", "Resume preparation is already running.")
        try:
            resumes = store.resumes()
            if not resumes:
                return redirect("/profile", "Upload a resume before preparing applications.", error=True)
            profile = effective_profile(store.profile(), resumes)
            configuration = resume_configuration(profile, resumes)
            items = review_items(profile, resumes)
            updates, errors = [], []
            for job in items:
                try:
                    proposal = await asyncio.to_thread(
                        prepare_resume, resumes, job.get("description", ""), profile["verified_skills"],
                        profile["tailor"], store.directory / "variants",
                    )
                    updates.append((job["id"], asdict(proposal), job_version(job), configuration))
                except ResumeError as exc:
                    errors.append(f"{job['title']}: {exc}")
            store.save_proposals(updates)
            return redirect("/review", " ".join([f"Prepared {len(updates)} applications.", *errors])[:650],
                            error=bool(errors))
        finally:
            prepare_lock.release()

    @app.post("/review/approve")
    async def approve(request: Request):
        form = await request.form()
        check_form(form)
        try:
            selected = {int(value): form_text(form, f"approval_{value}", 200) for value in form.getlist("selected")}
            batch_id = store.approve(selected, application_answers, approval_token, resolve_profile=effective_profile)
            await asyncio.to_thread(run_queue)
            await asyncio.to_thread(export_reports)
            return redirect("/applications", f"Batch {batch_id} processed. Check the status of each application below.")
        except ValueError as exc:
            return redirect("/review", str(exc), error=True)

    def pdf_response(path, name):
        path = Path(path).resolve()
        if not path.is_relative_to(store.directory) or not path.is_file():
            raise HTTPException(404, "This PDF could not be found.")
        return FileResponse(path, media_type="application/pdf", filename=name, content_disposition_type="inline")

    @app.get("/profile/resumes/{resume_id}/pdf")
    def original_pdf(resume_id: int):
        resume = next((row for row in store.resumes() if row["id"] == resume_id), None)
        if not resume:
            raise HTTPException(404)
        return pdf_response(resume["path"], resume["name"])

    @app.get("/review/{job_id}/pdf")
    def proposal_pdf(job_id: int):
        job = next((row for row in store.jobs() if row["id"] == job_id and row["proposal"]), None)
        if not job:
            raise HTTPException(404)
        return pdf_response(job["proposal"]["pdf_path"], job["proposal"]["resume_name"])

    @app.get("/applications")
    def applications_page(request: Request):
        applications = store.applications()
        external = [job for job in store.jobs() if job["manually_applied"] and job["application_status"] != "submitted"]
        return render(request, "applications", applications=applications, external=external)

    @app.get("/applications/{application_id}/pdf")
    def approved_pdf(application_id: int):
        row = next((row for row in store.applications() if row["id"] == application_id), None)
        if not row:
            raise HTTPException(404)
        return pdf_response(row["snapshot"]["pdf_path"], row["snapshot"]["original_resume"])

    @app.post("/applications/{application_id}/{action}")
    async def application_action(application_id: int, action: str, request: Request):
        form = await request.form()
        check_form(form)
        try:
            if action not in {"confirm", "skip", "resume", "unconfirmed", "receipt"}:
                raise ValueError("Unknown application action.")
            application = next((row for row in store.applications() if row["id"] == application_id), None)
            if not application:
                raise ValueError("Application not found.")
            receipt = None
            if action == "receipt":
                if application["status"] != "needs_verification" or not application["snapshot"]["test_scenario"]:
                    raise ValueError("This application has no test-portal receipt to check.")
                receipt = portal.receipt_for(application_id)
                if not receipt:
                    raise ValueError("The test portal has no receipt for this application.")
                action = "confirm"
            if action == "resume":
                if application["status"] != "needs_action" or not application["snapshot"]["test_scenario"]:
                    raise ValueError("This application has no test-portal challenge.")
                portal.complete_challenge(application_id)
            store.change_application(application_id, action, receipt)
            if action == "resume":
                await asyncio.to_thread(run_queue)
            await asyncio.to_thread(export_reports)
            return redirect("/applications", "Application updated.")
        except ValueError as exc:
            return redirect("/applications", str(exc), error=True)

    @app.get("/exports/{filename}")
    def download_report(filename: str):
        if filename not in {"jobs.html", "applications.html"}:
            raise HTTPException(404)
        with export_lock:
            path = store.directory / "exports" / filename
            return FileResponse(path, media_type="text/html; charset=utf-8", filename=filename)

    return app


def launch():
    import uvicorn
    uvicorn.run("switchmaxer.main:create_app", factory=True, host="127.0.0.1", port=8765)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m switchmaxer.main",
        description="Start the local SwitchMaxer dashboard at http://127.0.0.1:8765.",
    )
    parser.parse_args()
    launch()


if __name__ == "__main__":
    main()
