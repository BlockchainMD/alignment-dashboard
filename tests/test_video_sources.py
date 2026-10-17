import gzip
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import video_sources as sources


PARAGRAPH = (
    "Researchers compare a proposed explanation with the original system by making predictions on fresh examples. "
    "An explanation can sound convincing while missing details that influence later behavior. "
    "The useful test is whether its predictions agree with the system under the conditions that matter."
)


class SourceCaptureTests(unittest.TestCase):
    def setUp(self):
        (sources.ROOT / "artifacts").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=sources.ROOT / "artifacts")
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "capture"
        self.article = {"id": "article-1", "title": "A public research finding", "url": "https://example.org/research", "authors": ["A Researcher"], "summary": "This discovery excerpt must never become source evidence. " * 20}

    def response(self, body, *, url=None, kind="text/html"):
        return {"status": 200, "url": url or self.article["url"], "headers": {"Content-Type": kind}, "body": body.encode() if isinstance(body, str) else body}

    def test_main_article_capture_preserves_paragraphs_without_surrounding_navigation(self):
        body = f"<html><body><nav>Unrelated navigation</nav><main><article><h2>A useful question</h2><p>{PARAGRAPH}</p><p>Independent <em>holdout</em> examples provide a check.</p><script>Secret script text</script></article></main><footer>Newsletter sign-up</footer></body></html>"
        source = sources.capture_source(self.article, self.destination, lambda *args, **kwargs: self.response(body))
        self.assertEqual(source["content_kind"], "html")
        self.assertEqual([p["id"] for p in source["sections"]], ["p001", "p002", "p003"])
        self.assertEqual(source["sections"][1]["text"], PARAGRAPH)
        self.assertNotIn("navigation", source["text"])
        self.assertNotIn("Secret", source["text"])
        self.assertNotIn("Newsletter", source["text"])
        self.assertEqual(source["sha256"], hashlib.sha256(source["text"].encode()).hexdigest())
        self.assertEqual(json.loads((self.destination / "source.json").read_text()), source)

    def test_identical_evidence_has_identical_digest_despite_layout_whitespace(self):
        first = sources.capture_source(self.article, self.destination, lambda *a, **k: self.response(f"<article><p>{PARAGRAPH}</p></article>"))
        second = sources.capture_source(self.article, self.destination, lambda *a, **k: self.response(f"<article>\n<p>  {PARAGRAPH.replace(' ', '  ')}\n</p>\n</article>"))
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["sections"], second["sections"])

    def test_excerpt_never_substitutes_for_missing_original(self):
        with self.assertRaisesRegex(sources.SourceCaptureError, "too short"):
            sources.capture_source(self.article, self.destination, lambda *a, **k: self.response("<article>Access restricted.</article>"))
        self.assertFalse((self.destination / "source.json").exists())

    def test_full_evidence_cannot_be_written_to_public_site(self):
        with self.assertRaisesRegex(sources.SourceCaptureError, "artifacts"):
            sources.capture_source(self.article, sources.ROOT / "site/data/source", lambda *a, **k: self.fail("Should reject before fetching"))

    def test_forum_requests_one_post_and_uses_complete_revision_instead_of_capped_excerpt(self):
        article = {**self.article, "url": "https://www.lesswrong.com/posts/Abc123/research"}
        complete = PARAGRAPH + " " + "A further measured observation follows. " * 65
        payload = {"data": {"post": {"result": {"_id": "Abc123", "title": "Original title", "pageUrl": article["url"], "contents": {"plaintextMainText": complete[:2000], "html": f"<h2>The question</h2><p>{complete}</p><p>The final qualification remains important.</p>"}, "user": {"displayName": "Original Author"}}}}}
        requested = []

        def fetch(url, **kwargs):
            requested.append(url)
            return self.response(json.dumps(payload), url=url, kind="application/json")

        source = sources.capture_source(article, self.destination, fetch)
        query = parse_qs(urlsplit(requested[0]).query)["query"][0]
        self.assertIn('selector:{_id:"Abc123"}', query)
        self.assertIn("plaintextMainText html", query)
        self.assertEqual(source["content_kind"], "forum")
        self.assertEqual(source["authors"], ["Original Author"])
        self.assertEqual(source["title"], "Original title")
        self.assertIn("The final qualification", source["text"])
        self.assertGreater(len(source["text"]), 2000)

    def test_forum_truncated_plaintext_without_complete_html_fails(self):
        article = {**self.article, "url": "https://www.alignmentforum.org/posts/Abc123/research"}
        payload = {"data": {"post": {"result": {"_id": "Abc123", "contents": {"plaintextMainText": (PARAGRAPH * 20)[:2000]}}}}}
        with self.assertRaisesRegex(sources.SourceCaptureError, "truncated"):
            sources.capture_source(article, self.destination, lambda url, **k: self.response(json.dumps(payload), url=url, kind="application/json"))

    def test_forum_wrong_post_and_api_errors_are_rejected(self):
        article = {**self.article, "url": "https://www.lesswrong.com/posts/Abc123/research"}
        payload = {"errors": [{"message": "Unavailable"}], "data": {"post": {"result": {"_id": "Different", "contents": {"plaintextMainText": PARAGRAPH}}}}}
        with self.assertRaisesRegex(sources.SourceCaptureError, "requested public post"):
            sources.capture_source(article, self.destination, lambda url, **k: self.response(json.dumps(payload), url=url, kind="application/json"))

    def test_arxiv_abstract_is_replaced_with_original_pdf(self):
        article = {**self.article, "url": "https://arxiv.org/abs/2601.01234v2"}
        requested = []

        def fetch(url, **kwargs):
            requested.append(url)
            return self.response(b"%PDF-fixture", url=url, kind="application/pdf")

        with patch.object(sources, "_pdf_paragraphs", return_value=[PARAGRAPH]):
            source = sources.capture_source(article, self.destination, fetch)
        self.assertEqual(requested, ["https://arxiv.org/pdf/2601.01234v2"])
        self.assertEqual(source["content_kind"], "pdf")

    def test_scholarly_pdf_failure_does_not_use_abstract_html(self):
        for url in ["https://arxiv.org/abs/2601.01234", "https://openreview.net/forum?id=paper123"]:
            article = {**self.article, "url": url}
            with self.subTest(url=url), self.assertRaisesRegex(sources.SourceCaptureError, "no abstract"):
                sources.capture_source(article, self.destination, lambda url, **k: self.response(f"<article><p>{PARAGRAPH}</p></article>", url=url))

    def test_publisher_pdf_metadata_selects_full_paper(self):
        responses = {
            self.article["url"]: self.response(f'<meta name="citation_pdf_url" content="/paper.pdf"><article>{PARAGRAPH}</article>'),
            "https://example.org/paper.pdf": self.response(b"%PDF-fixture", url="https://example.org/paper.pdf", kind="application/pdf"),
        }
        with patch.object(sources, "_pdf_paragraphs", return_value=[PARAGRAPH]):
            source = sources.capture_source(self.article, self.destination, lambda url, **k: responses[url])
        self.assertEqual(source["url"], "https://example.org/paper.pdf")
        self.assertEqual(source["content_kind"], "pdf")

    def test_missing_pdf_dependency_has_clear_failure(self):
        with patch.dict(sys.modules, {"pypdf": None}):
            with self.assertRaisesRegex(sources.SourceCaptureError, "requires pypdf"):
                sources._pdf_paragraphs(b"%PDF-fixture")

    def test_binary_or_malformed_responses_are_not_source_text(self):
        with self.assertRaisesRegex(sources.SourceCaptureError, "not supported HTML"):
            sources.capture_source(self.article, self.destination, lambda *a, **k: self.response(b"binary", kind="application/octet-stream"))
        with self.assertRaisesRegex(sources.SourceCaptureError, "missing bytes"):
            sources.capture_source(self.article, self.destination, lambda *a, **k: {"body": "unbounded text"})


class PublicFetchSafetyTests(unittest.TestCase):
    def test_credentials_private_literals_and_non_web_schemes_are_rejected(self):
        unsafe = [
            "file:///etc/passwd", "https://user:password@example.org/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
            "https://[::1]/", "http://10.0.0.1/", "http://localhost/", "http://192.168.1.1/", "https://example.org:8080/", "https://example.org/\nheader",
        ]
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(sources.SourceCaptureError):
                sources._validated_url(url)

    def test_any_private_dns_address_rejects_the_host(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(socket, "getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(sources.SourceCaptureError, "private or reserved"):
                sources._public_addresses("example.org", 443)

    def test_connection_uses_the_validated_ip_without_resolving_again(self):
        class FakeSocket:
            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, address):
                self.address = address

        fake = FakeSocket()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        with patch.object(socket, "socket", return_value=fake), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS must stay pinned")):
            connection = sources._PinnedHTTPConnection("example.org", 80, addresses, secure=False, timeout=1)
            connection.connect()
        self.assertEqual(fake.address, ("93.184.216.34", 80))

    def test_private_redirect_is_rejected_before_following_it(self):
        class Response:
            status = 302

            def getheaders(self):
                return [("Location", "http://127.0.0.1/secret")]

        class Connection:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with patch.object(sources, "_public_addresses", return_value=[]), patch.object(sources, "_PinnedHTTPConnection", Connection):
            with self.assertRaisesRegex(sources.SourceCaptureError, "private or reserved"):
                sources.fetch_public("https://example.org/article")

    def test_response_and_decompression_limits_are_enforced(self):
        class Response:
            def __init__(self):
                self.body = io.BytesIO(b"x" * 33)

            def getheader(self, key):
                return None

            def read1(self, count):
                return self.body.read(count)

        with patch.object(sources, "MAX_BYTES", 32):
            with self.assertRaisesRegex(sources.SourceCaptureError, "exceeds"):
                sources._bounded_body(Response(), time.monotonic() + 1)
            with self.assertRaisesRegex(sources.SourceCaptureError, "exceeds"):
                sources._decoded_body(gzip.compress(b"x" * 1000), {"content-encoding": "gzip"})

    def test_cookies_and_authorization_headers_are_not_accepted(self):
        with self.assertRaisesRegex(sources.SourceCaptureError, "credential"):
            sources.fetch_public("https://example.org/article", headers={"Authorization": "Bearer unused"})


if __name__ == "__main__":
    unittest.main()
