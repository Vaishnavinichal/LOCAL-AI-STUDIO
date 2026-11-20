import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import testing_engine


class TestDiscoveryTests(unittest.TestCase):
    def test_discovers_python_tests_syntax_and_missing_pytest_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='sample'\n[project.optional-dependencies]\ntest=['pytest']\n", encoding="utf-8")
            (root / "test_sample.py").write_text("def test_example(): pass\n", encoding="utf-8")
            with patch("app.testing_engine.shutil.which", side_effect=lambda name: "python.exe" if name == "python" else None), \
                 patch("app.testing_engine.importlib.util.find_spec", return_value=None):
                result = testing_engine.discover(root)
            suites = {item["id"]: item for item in result["suites"]}
            self.assertIn("test_sample.py", result["test_files"])
            self.assertFalse(suites["python-pytest"]["available"])
            self.assertIn("not available", suites["python-pytest"]["missing_reason"])
            self.assertTrue(suites["python-unittest"]["available"])
            self.assertIn("python-syntax", suites)
            self.assertFalse((root / "node_modules").exists())

    def test_discovers_npm_vitest_jest_build_and_typecheck_from_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "node_modules/.bin").mkdir(parents=True)
            (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "1", "typescript": "5"}, "scripts": {"test": "vitest", "build": "vite build", "typecheck": "tsc --noEmit"}}), encoding="utf-8")
            (root / "src.test.ts").write_text("test('ok', () => {});\n", encoding="utf-8")
            with patch("app.testing_engine.shutil.which", side_effect=lambda name: "node.exe" if name == "node" else None):
                result = testing_engine.discover(root)
            suites = {item["id"]: item for item in result["suites"]}
            self.assertEqual(suites["npm-test:." ]["framework"], "vitest")
            self.assertEqual(suites["npm-test:." ]["command"], "npm.cmd test -- --run")
            self.assertIn("npm-build:.", suites)
            self.assertIn("npm-typecheck:.", suites)
            self.assertTrue(suites["npm-test:." ]["available"])

    def test_classifies_pytest_unittest_jest_vitest_and_node_tap_results(self):
        pytest_result = testing_engine.classify("FAILED tests/test_x.py::test_x - AssertionError\n1 failed, 2 passed, 1 skipped", command="pytest", suite_kind="tests", exit_code=1)
        self.assertEqual(pytest_result["status"], "failed")
        self.assertEqual(pytest_result["counts"], {"passed": 2, "failed": 1, "skipped": 1, "total": 4})
        self.assertEqual(testing_engine.classify("Ran 1 test\n\nOK", command="unittest", suite_kind="tests", exit_code=0)["status"], "passed")
        skipped = testing_engine.classify("Ran 2 tests\n\nOK (skipped=1)", command="unittest", suite_kind="tests", exit_code=0)
        self.assertEqual(skipped["status"], "passed_with_skips")
        self.assertEqual(skipped["counts"], {"passed": 1, "failed": None, "skipped": 1, "total": 2})
        self.assertEqual(testing_engine.classify("Tests: 1 failed, 2 passed, 1 skipped, 4 total", command="jest", suite_kind="tests", exit_code=1)["counts"]["total"], 4)
        self.assertEqual(testing_engine.classify("Tests  1 failed | 2 passed (3)", command="vitest", suite_kind="tests", exit_code=1)["counts"]["passed"], 2)
        self.assertEqual(testing_engine.classify("# tests 1\n# pass 1\n# fail 0", command="npm test", suite_kind="tests", exit_code=0)["status"], "passed")
        self.assertEqual(testing_engine.classify("No module named missing_dependency", command="python -m pytest", suite_kind="tests", exit_code=1)["category"], "missing_dependency")
        unavailable = testing_engine.classify("No module named missing_dependency", command="python -m pytest", suite_kind="tests", exit_code=1)
        self.assertEqual(unavailable["status"], "error", "A missing dependency is not a failed test")
        syntax_collection = testing_engine.classify("ERROR collecting tests/test_x.py\nSyntaxError: invalid syntax", command="pytest", suite_kind="tests", exit_code=2)
        self.assertEqual(syntax_collection["status"], "error", "A collection error is not a failed test")
        self.assertEqual(testing_engine.classify("SyntaxError: invalid syntax", command="python -m compileall", suite_kind="syntax", exit_code=1)["category"], "syntax_error")

    def test_maps_source_locations_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "sample.py"
            source.write_text("def calculate():\n    password = 'do-not-leak'\n    return 2\n", encoding="utf-8")
            failure = testing_engine.classify(f'File "{source}", line 3, in calculate\nAssertionError: expected 3, got 2\napi_key=secret-value', command="pytest", suite_kind="tests", exit_code=1)
            related = testing_engine.related_context(root, failure)
            self.assertEqual(failure["location"], {"path": str(source), "line": 3})
            self.assertEqual(related[0]["path"], "sample.py")
            self.assertNotIn("secret-value", failure["output"])

    def test_repair_loop_stops_at_limit_and_when_the_same_failure_repeats(self):
        failure = {"category": "assertion_failure", "failed_test": "test_x", "message": "expected 1 got 2"}
        attempt = {"failure_signature": testing_engine.failure_signature(failure), "retest_run_id": "rerun"}
        self.assertEqual(testing_engine.repair_stop_reason(failure, [], 2), None)
        self.assertEqual(testing_engine.repair_stop_reason(failure, [attempt], 2), "repeated_failure")
        self.assertEqual(testing_engine.repair_stop_reason(failure, [attempt, attempt], 2), "attempt_limit")


if __name__ == "__main__":
    unittest.main()
