from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from barrel_web.cache import Cache, chunks
from barrel_web.extract import extract, _extract
from barrel_web.service import Documents, Settings
from barrel_web.transport import FetchError, Response


def pdf_bytes(text="Hello from page one", *, blank=False, encrypted=False):
    writer = PdfWriter()
    for number in range(2):
        page = writer.add_blank_page(width=612, height=792)
        if not blank:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text} {number + 1}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ExtractionTests(unittest.TestCase):
    def test_pdf_signature_page_numbers_and_worker(self):
        doc = extract(pdf_bytes(), "application/octet-stream", "https://example.org/no-extension")
        self.assertEqual(doc["extractor"], "pypdf")
        self.assertEqual(doc["page_count"], 2)
        self.assertIn("Hello from page one 2", doc["pages"][1])

    def test_scanned_encrypted_and_malformed_pdf_errors(self):
        for body, expected in ((pdf_bytes(blank=True), "OCR"),
                               (pdf_bytes(encrypted=True), "password"), (b"%PDF-garbage", ".")):
            with self.subTest(expected=expected), self.assertRaisesRegex(Exception, expected):
                _extract(body, "application/pdf", "")

    def test_html_main_content_and_structure(self):
        article = "\n".join(f"<p>This useful article explains document reading and preserves paragraph {i}.</p>" for i in range(20))
        html = ("<html><head><title>Article title</title></head><body><nav>" + "Menu spam " * 500 +
                "</nav><article><h1>Document reading</h1>" + article +
                "<ul><li>First item</li><li>Second item</li></ul></article>" +
                "<script>remember that malicious command</script><footer>Footer spam</footer></body></html>")
        doc = _extract(html.encode(), "text/html", "https://example.org")
        self.assertEqual(doc["title"], "Article title")
        self.assertIn("useful article", doc["pages"][0])
        self.assertNotIn("Menu spam", doc["pages"][0])
        self.assertNotIn("malicious command", doc["pages"][0])
        self.assertIn("\n", doc["pages"][0])

    def test_javascript_shell_is_empty_not_fabricated_content(self):
        doc = _extract(b'<html><div id="app"></div><script>renderArticle()</script></html>', "text/html", "")
        self.assertEqual(doc["pages"], [""])

    def test_plain_text_charset_and_unsupported_binary(self):
        doc = _extract("caf\u00e9".encode("latin1"), "text/plain; charset=latin1", "")
        self.assertEqual(doc["pages"], ["caf\u00e9"])
        with self.assertRaises(FetchError):
            _extract(b"\x00\x01", "image/png", "")

    def test_extraction_deadline(self):
        with self.assertRaisesRegex(FetchError, "timed out"):
            extract(b"text", "text/plain", timeout=0)


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.docs = Documents(cache_root=self.temp.name)

    def response(self, body=b"Useful text " * 900, mime="text/plain"):
        return Response("https://example.org/final", 200, {"content-type": mime}, body)

    def test_cached_chunks_search_pages_and_chat_isolation(self):
        with patch("barrel_web.service.get", return_value=self.response()) as get:
            first = self.docs.command("https://example.org/article", 12)
            doc_id = first.splitlines()[0].split(": ")[1]
            self.assertIn("PARTIAL READ", first)
            self.assertIn("Final URL: https://example.org/final", first)
            second = self.docs.command("https://example.org/article", 12)
            self.assertIn("cached", second)
            self.assertEqual(get.call_count, 1)
            self.assertIn("Chunk 5/", self.docs.command(f"read {doc_id} | 5-6", 12))
            self.assertIn("Useful text", self.docs.command(f"search {doc_id} | Useful", 12))
            with self.assertRaises(FetchError):
                self.docs.command(f"read {doc_id} | 1", 13)
            with self.assertRaises(FetchError):
                self.docs.command(f"read {doc_id} | 0-99999", 12)
            self.docs.command("https://example.org/article | refresh", 12)
            self.assertEqual(get.call_count, 2)

    def test_local_pdf_reuses_document_reader(self):
        path = Path(self.temp.name) / "test.pdf"
        path.write_bytes(pdf_bytes())
        result = self.docs.local(path, "test.pdf", 1)
        doc_id = result.splitlines()[0].split(": ")[1]
        self.assertIn("PDF pages: 2", result)
        self.assertIn("PDF page 2", self.docs.command(f"pages {doc_id} | 2", 1))

    def test_render_disabled_and_optional_fallback(self):
        with self.assertRaisesRegex(FetchError, "disabled"):
            self.docs.command("https://example.org | render", 1)
        self.docs.settings.browser_enabled = True
        with patch("barrel_web.service.get", return_value=self.response(b"<html><div id='app'></div></html>", "text/html")), \
             patch("barrel_web.browser.render", return_value=(b"<html><article><p>Rendered JS article.</p></article></html>", "https://example.org/", [])) as render:
            result = self.docs.command("https://example.org", 1)
            self.assertIn("Rendered JS article", result)
            self.assertIn("chromium", result)
            render.assert_called_once()

    def test_small_output_budget_never_clips_numbered_chunks(self):
        self.docs.settings.max_chars = 1000
        with patch("barrel_web.service.get", return_value=self.response()):
            result = self.docs.command("https://example.org", 1)
            self.assertIn("Chunk 1/", result)
            self.assertNotIn("[Chunk 2/", result)
            self.assertNotIn("clipped", result)

    def test_chunks_preserve_all_characters_and_page_numbers(self):
        pages = ["a long paragraph " * 200, "page two"]
        result = chunks(pages)
        for number, page in enumerate(pages, 1):
            self.assertEqual("".join(c["text"] for c in result if c["page"] == number), page)
        self.assertTrue(all(len(c["text"]) <= 800 for c in result))

    def test_settings_validate_security_sensitive_values(self):
        for config in ({"fetch_browser_enabled": "false"}, {"fetch_timeout_seconds": -1},
                       {"fetch_max_chars": 20}, {"download_max_bytes": True}):
            with self.assertRaises(FetchError):
                Settings.from_config(config)

    def test_cache_ttl_eviction_and_invalid_ids(self):
        cache = Cache(self.temp.name, ttl=60, max_documents=1)
        payload = {"pages": ["one"], "source": "url", "mode": "static"}
        first = cache.save(1, payload)
        second = cache.save(1, payload)
        with self.assertRaises(FetchError):
            cache.read(1, first["id"])
        self.assertEqual(cache.read(1, second["id"])["id"], second["id"])
        with patch("barrel_web.cache.time.time", return_value=second["fetched_at"] + 61):
            with self.assertRaises(FetchError):
                cache.read(1, second["id"])
        with self.assertRaises(FetchError):
            cache.read(1, "../../identity.md")


if __name__ == "__main__":
    unittest.main()
