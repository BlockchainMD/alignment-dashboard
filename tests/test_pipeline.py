import unittest
from scripts.export_video_jobs import make_packet
from scripts.check_health import coverage_failures, CORE_SOURCES


class PipelineTests(unittest.TestCase):
    def test_collection_time_changes_do_not_duplicate_video_jobs(self):
        article = {"id": "paper", "title": "A research finding", "url": "https://example.org/paper", "summary": "An abstract.", "provenance": [{"source_id": "one", "article_url": "https://example.org/paper", "found_at": "2026-10-15", "last_seen": "2026-10-15"}]}
        first = make_packet(article)
        article["provenance"][0]["last_seen"] = "2026-10-16"
        self.assertEqual(first["job_id"], make_packet(article)["job_id"])
        article["summary"] = "The publisher corrected the abstract."
        self.assertNotEqual(first["job_id"], make_packet(article)["job_id"])

    def test_core_fallback_and_repeated_publisher_failure_trigger_attention(self):
        sources = [{"id": identifier, "status": "ok"} for identifier in CORE_SOURCES]
        self.assertEqual(coverage_failures({"sources": sources}), [])
        sources[0]["status"] = "degraded"
        self.assertEqual(len(coverage_failures({"sources": sources})), 1)
        sources.append({"id": "publisher", "status": "error", "failure_streak": 3})
        self.assertEqual(len(coverage_failures({"sources": sources})), 2)


if __name__ == "__main__":
    unittest.main()
