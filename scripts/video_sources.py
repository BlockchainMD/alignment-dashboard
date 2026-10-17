"""Capture complete public source evidence for local video production.

The archive's excerpts are discovery metadata, never video evidence. Captures
live beneath ignored ``artifacts/`` and are deliberately excluded from the site.
An injected fetcher has the same keyword interface as ``fetch_public`` and
returns ``{url, status, headers, body}``; tests can therefore avoid the network.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
from pathlib import Path
import re
import queue
import socket
import ssl
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
import zlib

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 25
MAX_REDIRECTS = 5
MIN_TEXT_CHARS = 200
USER_AGENT = "AlignmentVideoSourceCapture/1.0 (+https://blockchainmd.github.io/alignment-dashboard/)"


class SourceCaptureError(ValueError):
    """The complete public original could not be safely captured."""


def _validated_url(url):
    if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\x00-\x20\x7f]", url):
        raise SourceCaptureError("Source URL is malformed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SourceCaptureError("Source URL is malformed") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise SourceCaptureError("Source URL must use public HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SourceCaptureError("Source URL must not contain credentials")
    if port not in {None, 80, 443}:
        raise SourceCaptureError("Source URL must use a standard HTTP or HTTPS port")
    hostname = parsed.hostname.rstrip(".")
    if "%" in hostname or hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise SourceCaptureError("Source URL must not address a local host")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SourceCaptureError("Source URL hostname is invalid") from exc
    else:
        if not literal.is_global:
            raise SourceCaptureError("Source URL must not address a private or reserved IP")
    return parsed, hostname, port or (443 if parsed.scheme == "https" else 80)


def _public_addresses(hostname, port, timeout=TIMEOUT_SECONDS):
    # getaddrinfo has no portable timeout argument. A daemon resolver keeps even
    # a stalled system DNS lookup from blocking this capture indefinitely.
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=max(0.001, timeout))
    except queue.Empty as exc:
        raise SourceCaptureError("Source hostname resolution exceeded its time limit") from exc
    if isinstance(addresses, OSError):
        raise SourceCaptureError("Could not resolve the source hostname") from addresses
    if not addresses:
        raise SourceCaptureError("Source hostname resolved to no public addresses")
    for family, kind, protocol, _, address in addresses:
        try:
            public = ipaddress.ip_address(address[0]).is_global
        except ValueError:
            public = False
        if not public:
            raise SourceCaptureError("Source hostname resolves to a private or reserved IP")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname, port, addresses, *, secure, timeout):
        super().__init__(hostname, port, timeout=timeout)
        self._addresses = addresses
        self._secure = secure

    def connect(self):
        # Connect to the address already validated above. Resolving again inside
        # urllib would allow DNS rebinding between validation and connection.
        last_error = None
        deadline = time.monotonic() + self.timeout
        for family, kind, protocol, _, address in self._addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Source connection exceeded its time limit")
            connection = socket.socket(family, kind, protocol)
            connection.settimeout(remaining)
            try:
                connection.connect(address)
                if self._secure:
                    connection.settimeout(max(0.001, deadline - time.monotonic()))
                    connection = ssl.create_default_context().wrap_socket(connection, server_hostname=self.host)
                self.sock = connection
                return
            except OSError as exc:
                last_error = exc
                connection.close()
        raise last_error or OSError("No usable public address")


def _bounded_body(response, deadline):
    length = response.getheader("Content-Length")
    if length:
        try:
            if int(length) > MAX_BYTES:
                raise SourceCaptureError("Source response exceeds the 16 MiB limit")
        except ValueError as exc:
            if isinstance(exc, SourceCaptureError):
                raise
            raise SourceCaptureError("Source returned an invalid Content-Length") from exc
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            raise SourceCaptureError("Source request exceeded its time limit")
        # read1 returns after one buffered/socket read, so slow trickling cannot
        # postpone the wall-clock deadline until an entire 64 KiB block arrives.
        try:
            response.fp.raw._sock.settimeout(max(0.001, deadline - time.monotonic()))
        except AttributeError:
            pass
        chunk = response.read1(min(65536, MAX_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_BYTES:
            raise SourceCaptureError("Source response exceeds the 16 MiB limit")


def _decoded_body(body, headers):
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding in {"", "identity"}:
        return body
    if encoding not in {"gzip", "deflate"}:
        raise SourceCaptureError("Source uses an unsupported content encoding")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        decoded = decoder.decompress(body, MAX_BYTES + 1)
        if len(decoded) > MAX_BYTES or decoder.unconsumed_tail:
            raise SourceCaptureError("Decoded source response exceeds the 16 MiB limit")
        decoded += decoder.flush(MAX_BYTES + 1 - len(decoded))
        if len(decoded) > MAX_BYTES or not decoder.eof or decoder.unused_data:
            raise SourceCaptureError("Source compression is incomplete or exceeds the response limit")
        return decoded
    except zlib.error as exc:
        raise SourceCaptureError("Source returned invalid compressed content") from exc


def fetch_public(url, *, method="GET", data=None, headers=None):
    """Fetch a bounded anonymous public response, including safe redirects."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    current_url = url
    if method != "GET" or data is not None:
        raise SourceCaptureError("Source capture supports anonymous GET requests only")
    supplied = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    if set(supplied) - {"accept", "content-type"}:
        raise SourceCaptureError("Source capture does not accept credential or custom transport headers")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": supplied.get("accept", "*/*")}
    for redirect in range(MAX_REDIRECTS + 1):
        parsed, hostname, port = _validated_url(current_url)
        addresses = _public_addresses(hostname, port, deadline - time.monotonic())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceCaptureError("Source request exceeded its time limit")
        connection = _PinnedHTTPConnection(hostname, port, addresses, secure=parsed.scheme == "https", timeout=remaining)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request("GET", target, headers=request_headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                if redirect >= MAX_REDIRECTS or not response_headers.get("location"):
                    raise SourceCaptureError("Source has too many redirects or an invalid redirect")
                current_url = urljoin(current_url, response_headers["location"])
                _validated_url(current_url)
                continue
            if response.status != 200:
                raise SourceCaptureError(f"Source returned HTTP {response.status}")
            body = _decoded_body(_bounded_body(response, deadline), response_headers)
            return {"url": current_url, "status": response.status, "headers": response_headers, "body": body}
        except (OSError, http.client.HTTPException) as exc:
            raise SourceCaptureError(f"Could not retrieve the complete source: {type(exc).__name__}") from exc
        finally:
            connection.close()
    raise SourceCaptureError("Source has too many redirects")


def _fetch(fetcher, url, *, accept="*/*"):
    _validated_url(url)
    response = fetcher(url, method="GET", data=None, headers={"Accept": accept})
    if not isinstance(response, dict) or response.get("status", 200) != 200:
        raise SourceCaptureError("Source fetcher returned an unsuccessful response")
    final_url = response.get("url", url)
    _validated_url(final_url)
    body = response.get("body")
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise SourceCaptureError("Source response is missing bytes or exceeds the 16 MiB limit")
    return {"url": final_url, "headers": {str(k).lower(): str(v) for k, v in response.get("headers", {}).items()}, "body": body}


class _Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []


class _Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 256:
            raise SourceCaptureError("Source HTML exceeds the supported nesting depth")
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _normalize_paragraph(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def _html_paragraphs(html, *, fragment=False):
    document = _Document(html)
    nodes = list(_walk(document.root))
    selected = document.root
    if not fragment:
        selectors = [
            lambda n: n.attrs.get("itemprop") == "articleBody",
            lambda n: n.tag == "article",
            lambda n: n.tag == "main" or n.attrs.get("role") == "main",
            lambda n: any(part in {"post-content", "article-content", "entry-content", "blog-post-content", "markdown-body"} for part in n.attrs.get("class", "").split()),
            lambda n: n.tag == "body",
        ]
        for selector in selectors:
            candidates = [node for node in nodes if selector(node)]
            if candidates:
                selected = max(candidates, key=lambda node: sum(len(child) for part in _walk(node) for child in part.children if isinstance(child, str)))
                break
    drop = {"script", "style", "nav", "footer", "header", "form", "button", "aside", "noscript", "svg", "canvas", "template"}
    blocks = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr", "figure", "figcaption", "br", "hr"}
    paragraphs, buffer = [], []

    def flush():
        text = _normalize_paragraph("".join(buffer))
        if text:
            paragraphs.append(text)
        buffer.clear()

    def visit(node):
        if isinstance(node, str):
            buffer.append(node)
            return
        if node.tag in drop or "hidden" in node.attrs or node.attrs.get("aria-hidden") == "true":
            return
        if node.tag in blocks:
            flush()
        for child in node.children:
            visit(child)
        if node.tag in {"td", "th"}:
            buffer.append(" ")
        if node.tag in blocks:
            flush()

    visit(selected)
    flush()
    return paragraphs, document


def _decode_html(response):
    content_type = response["headers"].get("content-type", "")
    charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    try:
        return response["body"].decode(charset.group(1) if charset else "utf-8")
    except (LookupError, UnicodeDecodeError) as exc:
        raise SourceCaptureError("Source HTML does not have a supported text encoding") from exc


def _pdf_paragraphs(body):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SourceCaptureError("PDF source extraction requires pypdf; install the video pipeline requirements and retry. No abstract was substituted.") from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            raise SourceCaptureError("The PDF source is encrypted and cannot be read anonymously")
        if len(reader.pages) > 500:
            raise SourceCaptureError("The PDF source exceeds the 500-page extraction limit")
        paragraphs = []
        for page in reader.pages:
            text = page.extract_text() or ""
            paragraphs.extend(part for block in re.split(r"\n\s*\n", text) if (part := _normalize_paragraph(block)))
        return paragraphs
    except SourceCaptureError:
        raise
    except Exception as exc:
        raise SourceCaptureError(f"Could not extract the complete PDF source: {type(exc).__name__}; no abstract was substituted") from exc


def _forum_source(url, fetcher):
    parsed, _, _ = _validated_url(url)
    if parsed.hostname not in {"lesswrong.com", "www.lesswrong.com", "alignmentforum.org", "www.alignmentforum.org"}:
        return None
    match = re.match(r"/posts/([A-Za-z0-9]+)(?:/|$)", parsed.path)
    if not match:
        raise SourceCaptureError("A forum source must point to an individual public post")
    post_id = match.group(1)
    # plaintextMainText is often a 2,000-character excerpt on this API. Request
    # the complete revision HTML too and use it whenever it contains more text.
    query = "{post(input:{selector:{_id:" + json.dumps(post_id) + "}}){result{_id title pageUrl contents{plaintextMainText html} user{displayName}}}}"
    endpoint = f"https://{parsed.hostname}/graphql?" + urlencode({"query": query})
    response = _fetch(fetcher, endpoint, accept="application/json")
    try:
        payload = json.loads(response["body"])
        row = ((payload.get("data") or {}).get("post") or {}).get("result")
    except (ValueError, AttributeError) as exc:
        raise SourceCaptureError("Forum API returned invalid source evidence") from exc
    if payload.get("errors") or not isinstance(row, dict) or row.get("_id") != post_id:
        raise SourceCaptureError("Forum API did not return the requested public post")
    contents = row.get("contents") or {}
    if not isinstance(contents, dict):
        raise SourceCaptureError("Forum API returned malformed source contents")
    plaintext = contents.get("plaintextMainText")
    html = contents.get("html")
    if plaintext is not None and not isinstance(plaintext, str):
        raise SourceCaptureError("Forum API returned malformed source text")
    if html is not None and not isinstance(html, str):
        raise SourceCaptureError("Forum API returned malformed source HTML")
    plain_paragraphs = [_normalize_paragraph(part) for part in re.split(r"\n\s*\n", plaintext or "")]
    plain_paragraphs = [part for part in plain_paragraphs if part]
    html_paragraphs = _html_paragraphs(html, fragment=True)[0] if isinstance(html, str) and html else []
    if html_paragraphs:
        paragraphs = html_paragraphs if len("\n\n".join(html_paragraphs)) > len("\n\n".join(plain_paragraphs)) else plain_paragraphs
    elif len(plaintext or "") >= 2000:
        raise SourceCaptureError("Forum returned a potentially truncated excerpt without complete revision HTML")
    else:
        paragraphs = plain_paragraphs
    source_url = row.get("pageUrl") or url
    _validated_url(source_url)
    user = row.get("user") or {}
    author = user.get("displayName") if isinstance(user, dict) else None
    return {"paragraphs": paragraphs, "url": source_url, "kind": "forum", "title": row.get("title"), "authors": [author] if isinstance(author, str) and author.strip() else []}


def _document_source(url, article, fetcher):
    parsed, _, _ = _validated_url(url)
    requires_pdf = parsed.path.lower().endswith(".pdf") or "/pdf/" in parsed.path or parsed.path == "/pdf"
    # A scholarly landing-page abstract is insufficient. These providers expose
    # the original PDF at a deterministic public URL.
    if parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} and parsed.path.startswith("/abs/"):
        url = "https://arxiv.org/pdf/" + parsed.path.removeprefix("/abs/")
        requires_pdf = True
    elif parsed.hostname in {"openreview.net", "www.openreview.net"} and parsed.path == "/forum":
        identifier = parse_qs(parsed.query).get("id", [None])[0]
        if not identifier:
            raise SourceCaptureError("OpenReview source has no paper identifier")
        url = "https://openreview.net/pdf?" + urlencode({"id": identifier})
        requires_pdf = True
    response = _fetch(fetcher, url, accept="text/html,application/pdf;q=0.9")
    content_type = response["headers"].get("content-type", "").split(";")[0].strip().lower()
    is_pdf = response["body"].startswith(b"%PDF-")
    if content_type == "application/pdf" or is_pdf:
        if not is_pdf:
            raise SourceCaptureError("The declared PDF source is not a PDF document")
        return {"paragraphs": _pdf_paragraphs(response["body"]), "url": response["url"], "kind": "pdf"}
    if requires_pdf:
        raise SourceCaptureError("The original PDF could not be retrieved; no abstract was substituted")
    if content_type not in {"", "text/html", "application/xhtml+xml"}:
        raise SourceCaptureError("Source is not supported HTML or PDF content")
    paragraphs, document = _html_paragraphs(_decode_html(response))
    for node in _walk(document.root):
        if node.tag == "meta" and node.attrs.get("name", "").lower() in {"citation_pdf_url", "eprints.document_url"} and node.attrs.get("content"):
            pdf_url = urljoin(response["url"], node.attrs["content"])
            pdf = _fetch(fetcher, pdf_url, accept="application/pdf")
            if not pdf["body"].startswith(b"%PDF-"):
                raise SourceCaptureError("The publisher's original PDF was unavailable; no abstract was substituted")
            return {"paragraphs": _pdf_paragraphs(pdf["body"]), "url": pdf["url"], "kind": "pdf"}
    return {"paragraphs": paragraphs, "url": response["url"], "kind": "html"}


def capture_source(article: dict, output_dir: Path, fetcher=None) -> dict:
    """Retrieve and persist complete original evidence, or fail explicitly."""
    if not isinstance(article, dict) or not article.get("id") or not article.get("title"):
        raise SourceCaptureError("Source capture requires an article ID and title")
    destination = Path(output_dir).resolve()
    try:
        destination.relative_to((ROOT / "artifacts").resolve())
    except ValueError as exc:
        raise SourceCaptureError("Full source evidence must be saved beneath the ignored artifacts/ directory") from exc
    url = article.get("url")
    _validated_url(url)
    fetch = fetcher or fetch_public
    source = _forum_source(url, fetch) or _document_source(url, article, fetch)
    paragraphs = source["paragraphs"]
    text = "\n\n".join(paragraphs)
    if len(text) < MIN_TEXT_CHARS or len(text.split()) < 35:
        raise SourceCaptureError("Original source text is too short to establish complete article evidence; no archive excerpt was substituted")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise SourceCaptureError("Extracted source text exceeds the 16 MiB limit")
    authors = source.get("authors") or article.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    elif not isinstance(authors, list):
        authors = []
    result = {
        "schema_version": 1,
        "article_id": str(article["id"]),
        "title": source.get("title") or str(article["title"]),
        "url": source["url"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "content_kind": source["kind"],
        "sections": [{"id": f"p{index:03d}", "text": paragraph} for index, paragraph in enumerate(paragraphs, 1)],
        "text": text,
        "authors": [value.strip() for value in authors if isinstance(value, str) and value.strip()],
    }
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(destination / "source.json")
    return result
