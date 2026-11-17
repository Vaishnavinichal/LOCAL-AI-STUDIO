import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app import main


class FakeResponse:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = headers or {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://provider.invalid")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider failure", request=request, response=response)

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def __enter__(self): return self
    def __exit__(self, *_): return False

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.response


class SequenceClient:
    def __init__(self, *responses): self.responses = list(responses)
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def get(self, *args, **kwargs):
        response = self.responses.pop(0)
        return response


def http_error(status):
    request = httpx.Request("GET", "https://provider.invalid")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("provider failure", request=request, response=response)


class MarketplaceRouteTests(unittest.TestCase):
    def test_search_routes_are_registered(self):
        paths = main.app.openapi()["paths"]
        self.assertIn("/marketplace/search", paths)
        self.assertIn("/marketplace/details", paths)
        self.assertIn("/marketplace/versions", paths)
        self.assertIn("/marketplace/providers", paths)
        self.assertIn("get", paths["/marketplace/search"])

    def test_huggingface_success_parsing_and_query(self):
        fake = FakeClient(FakeResponse(data=[{
            "id": "author/model", "downloads": 123, "lastModified": "2026-01-01",
            "pipeline_tag": "text-generation", "tags": ["gguf"],
            "cardData": {"license": "apache-2.0", "model_type": "llama", "description": "A test model"}
        }]))
        with patch.object(main, "_marketplace_http", return_value=fake):
            result = main.marketplace_search("huggingface", "test", "text-generation", 10)
        self.assertEqual(result["results"][0]["id"], "author/model")
        self.assertEqual(result["results"][0]["architecture"], "llama")
        self.assertEqual(result["results"][0]["license"], "apache-2.0")
        self.assertTrue(result["results"][0]["installable"])

    def test_ollama_search_parses_supported_catalog_page_and_empty_results(self):
        html = '<a href="/library/qwen3">Qwen 3 model</a><a href="/library/qwen3/tags">tags</a>'
        with patch.object(main, "_marketplace_http", return_value=FakeClient(FakeResponse(text=html))):
            result = main.marketplace_search("ollama", "qwen3")
        self.assertEqual(result["results"][0]["id"], "qwen3")
        with patch.object(main, "_marketplace_http", return_value=FakeClient(FakeResponse(text="<html></html>"))):
            self.assertEqual(main.marketplace_search("ollama", "no-match")["results"], [])

    def test_ollama_versions_ignore_navigation_labels(self):
        html = '<a href="/library/smollm2:135m">smollm2:135m 271MB</a><a href="/download">Download</a>'
        with patch.object(main, "_marketplace_http", return_value=FakeClient(FakeResponse(text=html))):
            result = main.marketplace_details("ollama", "smollm2")
        self.assertEqual(result["versions"], ["smollm2:135m"])

    def test_ollama_variant_sizes_and_cloud_classification_use_library_metadata(self):
        tags = '<div class="group px-4 py-3"><a href="/library/local-model:latest">local-model:latest</a><span>4.0GB - 6.0GB · 32K context window</span></div><div class="group px-4 py-3"><a href="/library/local-model:q8_0">local-model:q8_0</a><span>8GB · 32K context window</span></div>'
        page = '<h2>A 9B local model.</h2><p>vision tools</p><p>32K context window</p>'
        with patch.object(main, "_marketplace_http", return_value=SequenceClient(FakeResponse(text=tags), FakeResponse(text=page))):
            local = main._ollama_library_metadata("local-model", refresh=True)
        self.assertEqual(local["variants"][0]["size"], "4.0GB–6.0GB")
        self.assertEqual(local["variants"][1]["size_bytes"], 8 * 1024 ** 3)
        self.assertEqual(local["parameter_count"], "9B")
        self.assertFalse(local["is_cloud"])

        cloud_tags = '<div class="group px-4 py-3"><a href="/library/cloud-model:cloud">cloud-model:cloud</a><span>1M context window · cloud</span></div>'
        cloud_page = '<h2>Cloud model</h2><p>763B parameters · cloud · 1M context window</p>'
        with patch.object(main, "_marketplace_http", return_value=SequenceClient(FakeResponse(text=cloud_tags), FakeResponse(text=cloud_page))):
            cloud = main._ollama_library_metadata("cloud-model", refresh=True)
        self.assertTrue(cloud["is_cloud"])
        self.assertIsNone(cloud["download_size"])
        self.assertEqual(cloud["parameter_count"], "763B")

    def test_ollama_wrapped_model_lists(self):
        for payload in ([{"name": "x"}], {"models": [{"name": "x"}]}, {"data": {"models": [{"name": "x"}]}}):
            self.assertEqual(main._ollama_model_list(payload), [{"name": "x"}])

    def test_huggingface_detail_reports_selected_file_lfs_size(self):
        fake = FakeClient(FakeResponse(data={
            "id": "author/model", "sha": "a" * 40,
            "cardData": {"license": "apache-2.0"},
            "siblings": [{"rfilename": "model-q4.gguf", "size": 777, "lfs": {"size": 123456, "oid": "b" * 64}},
                         {"rfilename": "model.safetensors", "size": 999999}]
        }))
        with patch.object(main, "_marketplace_http", return_value=fake):
            result = main.marketplace_details("huggingface", "author/model")
        self.assertEqual(result["files"][0]["size"], 123456)
        self.assertEqual(result["files"][0]["sha256"], "b" * 64)

    def test_empty_huggingface_results(self):
        with patch.object(main, "_marketplace_http", return_value=FakeClient(FakeResponse(data=[]))):
            self.assertEqual(main.marketplace_search("huggingface", "no-match")["results"], [])

    def test_provider_status_errors_are_distinguished(self):
        cases = [(404, 502, "provider_endpoint_not_found"),
                 (429, 429, "provider_rate_limited"),
                 (500, 502, "provider_server_error")]
        for upstream, expected_status, expected_code in cases:
            with self.subTest(upstream=upstream):
                with patch.object(main, "_marketplace_http", return_value=FakeClient(error=http_error(upstream))):
                    with self.assertRaises(HTTPException) as raised:
                        main.marketplace_search("huggingface", "model")
                self.assertEqual(raised.exception.status_code, expected_status)
                self.assertEqual(raised.exception.detail["code"], expected_code)

    def test_provider_timeout(self):
        with patch.object(main, "_marketplace_http", return_value=FakeClient(error=httpx.ReadTimeout("timed out"))):
            with self.assertRaises(HTTPException) as raised:
                main.marketplace_search("huggingface", "model")
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail["code"], "provider_timeout")

    def test_invalid_provider_json(self):
        error = json.JSONDecodeError("invalid", "", 0)
        with patch.object(main, "_marketplace_http", return_value=FakeClient(FakeResponse(data=error))):
            with self.assertRaises(HTTPException) as raised:
                main.marketplace_search("huggingface", "model")
        self.assertEqual(raised.exception.detail["code"], "invalid_provider_response")

    def test_invalid_provider_and_query_validation(self):
        with self.assertRaises(HTTPException) as raised:
            main.marketplace_search("unknown", "model")
        self.assertEqual(raised.exception.status_code, 400)
        schema = main.app.openapi()["paths"]["/marketplace/search"]["get"]["parameters"]
        query = next(p for p in schema if p["name"] == "q")
        self.assertEqual(query["schema"]["maxLength"], 200)


if __name__ == "__main__":
    unittest.main()
