from collections import Counter
import unittest
from urllib.parse import parse_qs, urlsplit

from switchmaxer.discovery import ATS, _feed_endpoint
from switchmaxer.starter_sources import starter_sources


class StarterSourceTests(unittest.TestCase):
    def test_india_profile_loads_the_verified_roster_with_india_filters(self):
        sources = starter_sources({"locations": "Bangalore, Pune, Remote (India)"})
        self.assertEqual(Counter(source["kind"] for source in sources), {
            "greenhouse": 44, "ashby": 24, "lever": 11, "smartrecruiters": 9, "html": 1,
        })
        endpoints = {_feed_endpoint(source["kind"], source) for source in sources if source["kind"] in ATS}
        self.assertEqual(len(endpoints), 88)
        for source in sources:
            if source["kind"] == "smartrecruiters":
                self.assertEqual(parse_qs(urlsplit(source["url"]).query)["country"], ["in"])
            if source["kind"] in ATS:
                self.assertFalse(source["crawl_permission"])
        sap, = (source for source in sources if source["kind"] == "html")
        self.assertEqual(sap, {
            "name": "SAP Careers", "kind": "html", "url": "https://jobs.sap.com/en/jobs/?location=Bangalore",
            "slug": "", "crawl_permission": True,
        })

    def test_unknown_and_mixed_markets_keep_the_public_feeds_global(self):
        for locations in ("", "Remote", "Bengaluru, London", "Bengaluru or Singapore", ["India", "Worldwide"]):
            with self.subTest(locations=locations):
                sources = starter_sources({"locations": locations})
                self.assertEqual(len(sources), 88)
                self.assertTrue(all(source["kind"] in ATS for source in sources))
                for source in sources:
                    if source["kind"] == "smartrecruiters":
                        self.assertNotIn("country", parse_qs(urlsplit(source["url"]).query))
