import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

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

    def test_smartrecruiters_public_list_and_detail_keep_official_links(self):
        source = {
            "kind": "smartrecruiters", "name": "Arista Networks",
            "url": "https://careers.smartrecruiters.com/AristaNetworks?q=Kernel+Engineer&country=IN&destination=INTERNAL",
        }
        listing = {
            "id": "123", "name": "Software Engineer II", "releasedDate": "2026-11-12T09:00:00Z",
            "location": {"fullLocation": "Bangalore, , India", "hybrid": True},
            "ref": "https://example.net/untrusted-detail-url",
        }
        url = "https://jobs.smartrecruiters.com/AristaNetworks/123-software-engineer"
        detail = {
            "id": "123", "postingUrl": url, "applyUrl": f"{url}?oga=true",
            "refNumber": "REF123", "active": True,
            "jobAd": {"sections": {
                "jobDescription": {"title": "Role", "text": "<p>Develop kernel tooling.</p>"},
                "qualifications": {"text": "<ul><li>2 years of experience in C++.</li></ul>"},
            }},
        }
        requests = []

        def fetch(endpoint, *, allowed, **kwargs):
            path = urlsplit(endpoint).path
            self.assertTrue(allowed(endpoint))
            self.assertFalse(allowed(endpoint.replace("api.smartrecruiters.com", "example.net")))
            self.assertFalse(allowed(endpoint.replace("https:", "http:")))
            self.assertFalse(allowed("https://api.smartrecruiters.com/v1/companies/Other/postings"))
            if "destination=PUBLIC" in endpoint:
                self.assertFalse(allowed(endpoint.replace("destination=PUBLIC", "destination=INTERNAL")))
            requests.append((path, parse_qs(urlsplit(endpoint).query)))
            response = detail if path.endswith("/123") else {"content": [listing], "totalFound": 1}
            return json.dumps(response).encode(), endpoint

        with patch.object(discovery, "_get", side_effect=fetch):
            result, = discovery.refresh_sources([source])
        path = "/v1/companies/AristaNetworks/postings"
        self.assertEqual(requests, [
            (path, {"country": ["in"], "q": ["Kernel Engineer"], "destination": ["PUBLIC"],
                    "limit": ["100"], "offset": ["0"]}),
            (f"{path}/123", {}),
        ])
        self.assertEqual((result.status, result.error), ("success", ""))
        job, = result.records
        self.assertEqual(job, discovery.JobRecord(
            "smartrecruiters", "AristaNetworks/123", "Arista Networks", "Software Engineer II",
            "Bangalore, India", url, f"{url}?oga=true",
            "Role\nDevelop kernel tooling.\n\nQualifications\n2 years of experience in C++.",
            "2026-11-12", "REF123", "hybrid",
        ))

        api_source = source | {"url": f"https://api.smartrecruiters.com{path}?offset=10&q=Kernel+Engineer&country=in"}
        self.assertEqual(
            discovery._feed_endpoint("smartrecruiters", api_source),
            discovery._feed_endpoint("smartrecruiters", source),
        )
        with self.assertRaises(ValueError):
            discovery._feed_endpoint("smartrecruiters", {"url": "https://jobs.smartrecruiters.com.example.net/Fake"})

    def test_smartrecruiters_bounds_list_pages_and_preserves_link_only_results(self):
        source = {"kind": "smartrecruiters", "name": "Eurofins", "url": "https://jobs.smartrecruiters.com/Eurofins"}
        requests = []

        def fetch(endpoint, **kwargs):
            requests.append(endpoint)
            params = parse_qs(urlsplit(endpoint).query)
            if params:
                start = int(params["offset"][0])
                response = {
                    "content": [{"id": str(index), "name": f"Engineer {index}"} for index in range(start, start + 2)],
                    "totalFound": 6,
                }
            else:
                response = {"id": "0", "jobAd": {"sections": {"jobDescription": {"text": "Public description."}}}}
            return json.dumps(response).encode(), endpoint

        with (patch.object(discovery, "_get", side_effect=fetch),
              patch.object(discovery, "_SMARTRECRUITERS_PAGE_SIZE", 2),
              patch.object(discovery, "_SMARTRECRUITERS_DETAIL_LIMIT", 1)):
            result, = discovery.refresh_sources([source])
        self.assertEqual(len(requests), 3)
        self.assertTrue(requests[1].endswith("limit=2&offset=2"))
        self.assertEqual(result.status, "success")
        self.assertIn("listing limit reached after 4 postings", result.error)
        self.assertIn("Up to 1 posting descriptions", result.error)
        self.assertIn("3 postings have a link and basic details only", result.error)
        self.assertEqual([job.title for job in result.records], [f"Engineer {index}" for index in range(4)])
        self.assertEqual(result.records[0].description, "Job description\nPublic description.")
        self.assertEqual(result.records[-1].apply_url, "https://jobs.smartrecruiters.com/Eurofins/3")
        self.assertEqual(result.records[-1].description, "")

    def test_smartrecruiters_skips_closed_details_and_retains_links_on_rate_limit(self):
        source = {"kind": "smartrecruiters", "name": "Brady", "slug": "BradyCorporation3"}
        requests = []

        def fetch(endpoint, **kwargs):
            key = urlsplit(endpoint).path.rsplit("/", 1)[-1]
            requests.append(key)
            if key == "postings":
                response = {"content": [{"id": str(index), "name": "Engineer"} for index in range(1, 6)]}
            elif key == "1":
                raise discovery._FetchError("HTTP 404", 404)
            elif key == "2":
                response = {"id": "2", "active": False}
            elif key == "3":
                raise discovery._FetchError("HTTP 429", 429)
            else:
                self.fail("SmartRecruiters details should stop after an HTTP 429.")
            return json.dumps(response).encode(), endpoint

        with patch.object(discovery, "_get", side_effect=fetch):
            result, = discovery.refresh_sources([source])
        self.assertEqual(requests, ["postings", "1", "2", "3"])
        self.assertEqual(result.status, "success")
        self.assertEqual([job.source_id for job in result.records], [
            "BradyCorporation3/3", "BradyCorporation3/4", "BradyCorporation3/5",
        ])
        self.assertTrue(all(not job.description for job in result.records))
        self.assertIn("HTTP 429", result.error)

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
