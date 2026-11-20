import os
import unittest
from unittest.mock import patch

from app import main
from fastapi.testclient import TestClient


class BrowserModeBackendTests(unittest.TestCase):
    def test_local_vite_origins_are_allowed_without_wildcard_cors(self):
        origins = main._browser_cors_origins()
        for origin in (
            "http://localhost:1420", "http://127.0.0.1:1420",
            "http://localhost:4173", "http://127.0.0.1:4173",
            "http://localhost:5173", "http://127.0.0.1:5173",
        ):
            self.assertIn(origin, origins)
        self.assertNotIn("*", origins)
        client = TestClient(main.app)
        response = client.options("/health", headers={
            "Origin": "http://localhost:1420",
            "Access-Control-Request-Method": "GET",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:1420")

    def test_additional_browser_origins_are_explicit_and_deduplicated(self):
        with patch.dict(os.environ, {"LOCAL_AI_CORS_ORIGINS": "https://studio.example.test, http://localhost:1420"}):
            origins = main._browser_cors_origins()
        self.assertIn("https://studio.example.test", origins)
        self.assertEqual(origins.count("http://localhost:1420"), 1)
        self.assertNotIn("*", origins)

if __name__ == "__main__":
    unittest.main()
