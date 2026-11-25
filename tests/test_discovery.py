import json
import unittest
from unittest.mock import patch

from switchmaxer import discovery


class DiscoveryTests(unittest.TestCase):
    def test_native_email_import_deduplicates_and_prefers_explicit_employer_link(self):
        email = b"""From: alerts@linkedin.com
Subject: Software jobs
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<p><a href="https://www.linkedin.com/comm/jobs/view/software-engineer-ii-at-example-12345?trk=alert">Software Engineer II</a></p>
<p>Example &middot; Bengaluru</p>
<a href="https://www.linkedin.com/jobs/view/12345?utm_campaign=alert">View job</a>
<a href="https://www.linkedin.com/jobs/">See all jobs</a>
"""
        records = discovery.parse_import(
            "linkedin", email, apply_url="https://jobs.lever.co/example/abc/apply",
        )
        self.assertEqual(len(records), 1)
        job = records[0]
        self.assertEqual(job.url, "https://www.linkedin.com/jobs/view/12345")
        self.assertEqual(job.title, "Software Engineer II")
        self.assertEqual(job.company, "Example")
        self.assertEqual(job.location, "Bengaluru")
        self.assertEqual(job.apply_url, "https://jobs.lever.co/example/abc/apply")
        self.assertEqual(discovery.canonical_url(job.apply_url), "https://jobs.lever.co/example/abc")

    def test_other_boards_import_only_listing_links(self):
        sources = {
            "naukri": "https://www.naukri.com/job-listings-software-engineer-12345?utm_source=alert",
            "instahyre": "https://www.instahyre.com/job-12345-software-engineer/",
        }
        for name, url in sources.items():
            with self.subTest(name=name):
                content = f"https://www.{name}.com/\n{url}"
                job, = discovery.parse_import(name, content)
                self.assertEqual(job.url, discovery.canonical_url(url))

    def test_feed_refresh_isolates_source_failures_and_keeps_date_provenance(self):
        def fetch(url, **kwargs):
            if "greenhouse" in url:
                data = {"jobs": [{"id": 1, "title": "Engineer", "updated_at": "2025-01-01",
                                  "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                                  "content": "&lt;p&gt;2 years of experience&lt;/p&gt;"}]}
            elif "/missing?" in url:
                raise discovery._FetchError("HTTP 404", 404)
            else:
                data = {"jobs": [{"title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/2",
                                  "applyUrl": "https://jobs.ashbyhq.com/acme/2/apply",
                                  "isListed": True, "workplaceType": "Hybrid",
                                  "publishedAt": "2025-02-10T15:50:28Z"}]}
            return json.dumps(data).encode(), url

        sources = [
            {"kind": "greenhouse", "name": "Acme", "url": "https://job-boards.greenhouse.io/acme"},
            {"kind": "lever", "name": "Other", "url": "https://jobs.lever.co/missing"},
            {"kind": "ashby", "name": "Acme", "url": "https://jobs.ashbyhq.com/acme"},
        ]
        with patch.object(discovery, "_get", side_effect=fetch):
            greenhouse, lever, ashby = discovery.refresh_sources(sources)
        self.assertEqual([greenhouse.status, lever.status, ashby.status], ["success", "failed", "success"])
        self.assertIsNone(greenhouse.records[0].published_date)
        self.assertEqual(greenhouse.records[0].description, "2 years of experience")
        self.assertEqual(ashby.records[0].published_date, "2025-02-10")
        self.assertEqual(ashby.records[0].work_mode, "hybrid")

    def test_greenhouse_recovers_geography_from_a_labeled_description_or_office(self):
        item = {
            "id": 1, "title": "Software Engineer",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "location": {"name": "Hybrid"},
        }
        cases = [
            ({"content": "<p>Location: Bengaluru (Hybrid mode)</p>"}, "Bengaluru (Hybrid mode)"),
            ({"content": "<p>Available Locations</p><p>Bengaluru, India</p>"}, "Bengaluru, India"),
            ({"content": "<p>Available Locations</p><p>Atlanta, US</p><p>Bengaluru, IN</p><p>About the Role</p>"},
             "Atlanta, US; Bengaluru, IN"),
            ({"offices": [{"name": "Hybrid"}, {"location": "Bangalore, IND"}]}, "Bangalore, IND"),
            ({"content": "<p>We have colleagues and customers in Bengaluru.</p>"}, "Hybrid"),
            ({"content": "<p>Available locations</p><p>We develop new products in India.</p>"}, "Hybrid"),
            ({"location": {"name": "Hyderabad"}, "content": "<p>Location: Bengaluru</p>"}, "Hyderabad"),
        ]
        for patch, location in cases:
            with self.subTest(location=location, fields=tuple(patch)):
                job = discovery._feed_job("greenhouse", "Acme", item | patch)
                self.assertEqual(job.location, location)
                self.assertEqual(job.work_mode, "" if location == "Hyderabad" else "hybrid")
        office = discovery._feed_job("greenhouse", "Acme", item | {"offices": [{"name": "Bangalore, IND"}]})
        self.assertTrue(discovery.matches_profile(office, {"locations": "India", "work_modes": "Hybrid"}))

    def test_compact_software_title_aliases_only_match_known_levels(self):
        profile = {"target_titles": "Software Engineer", "target_levels": "Mid", "experience_years": "2.5"}
        for title in ("SDE2 - API Platform", "SWEII, Backend", "Software Dev Engineer 2", "Software EngineerII"):
            with self.subTest(title=title):
                self.assertTrue(discovery.matches_profile({"title": title}, profile))
        for title in ("SDE3 - API Platform", "SWE22, Backend", "SDEAI - Backend"):
            with self.subTest(title=title):
                self.assertFalse(discovery.matches_profile({"title": title}, profile))

    def test_company_crawler_follows_scoped_links_and_applies_wildcard_robots(self):
        base = "https://example.com"
        seen = []
        pages = {
            f"{base}/robots.txt": "User-agent: *\nDisallow: /*private*",
            f"{base}/careers": ('<a href="/careers/jobs/1">Opening</a>'
                                '<a href="/careers/private/jobs/2">Opening</a>'
                                '<a href="/outside/jobs/3">Opening</a>'
                                '<a href="https://external.com/jobs/4">Opening</a>'),
            f"{base}/careers/jobs/1": """<script type="application/ld+json">
{"@graph": [{"@type":"JobPosting","title":"Engineer","description":"<p>Python</p>",
"identifier":{"value":"R-1"},"datePosted":"2025-01-15",
"jobLocation":{"address":{"addressLocality":"Bengaluru","addressCountry":"IN"}}}]}
</script>""",
        }

        def fetch(url, **kwargs):
            seen.append(url)
            return pages[url].encode(), url

        source = {"kind": "html", "name": "Example", "url": f"{base}/careers", "crawl_permission": True}
        with patch.object(discovery, "_get", side_effect=fetch), patch.object(discovery, "sleep"):
            result, = discovery.refresh_sources([source])
        self.assertEqual(seen, list(pages))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.records[0].requisition_id, "R-1")
        self.assertEqual(result.records[0].location, "Bengaluru, IN")

    def test_company_crawler_never_fetches_job_boards_or_unconfirmed_sites(self):
        sources = [
            {"kind": "html", "name": "LinkedIn", "url": "https://in.linkedin.com/jobs", "crawl_permission": True},
            {"kind": "html", "name": "Example", "url": "https://example.com/careers", "crawl_permission": False},
        ]
        with patch.object(discovery, "_get") as fetch:
            results = discovery.refresh_sources(sources)
        fetch.assert_not_called()
        self.assertEqual([result.status for result in results], ["manual", "manual"])

    def test_matching_resolves_title_level_and_city_aliases(self):
        job, = discovery.parse_import(
            "manual", url="https://example.com/job/1", company="Example Inc.",
            title="Software Engineer II", location="Bengaluru, IN",
            description="2–4 years of professional experience. 7 years preferred in Python.",
        )
        profile = {"target_titles": "SDE", "target_levels": "mid-level", "locations": "Bangalore",
                   "include_companies": "Example", "experience_years": "2.5"}
        self.assertTrue(discovery.matches_profile(job, profile))
        self.assertFalse(discovery.matches_profile(job, profile | {"experience_years": "5"}))
        self.assertFalse(discovery.matches_profile(job, profile | {"target_levels": "senior"}))
        self.assertEqual(discovery.experience_range("7 years preferred in Python."), (None, None))


if __name__ == "__main__":
    unittest.main()
