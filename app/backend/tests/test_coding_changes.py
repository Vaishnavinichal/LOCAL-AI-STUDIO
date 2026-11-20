import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import coding_changes


class CodingChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.storage = self.base / "history"
        (self.root / "existing.py").write_text("before = 1\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, changes):
        return coding_changes.prepare(self.root, changes, self.storage, session_id="session-test")

    def test_create_and_modify_are_reviewable_then_applied(self):
        proposal = self.prepare([
            {"operation": "modify", "path": "existing.py", "content": "after = 2\n", "summary": "Update value"},
            {"operation": "create", "path": "nested/new.py", "content": "created = True\n"},
        ])
        self.assertIn("-before = 1", proposal["changes"][0]["diff"])
        proposal = coding_changes.review_all(self.storage, proposal["id"], "approved")
        snapshots = []
        applied = coding_changes.apply(self.storage, proposal["id"], before_write=lambda root, path, content: snapshots.append((path, content)))
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "after = 2\n")
        self.assertEqual((self.root / "nested/new.py").read_text(encoding="utf-8"), "created = True\n")
        self.assertEqual(snapshots, [("existing.py", "before = 1\r\n")])
        self.assertEqual(applied["status"], "applied")

    def test_path_traversal_absolute_and_symlink_are_rejected(self):
        for value in ("../outside.txt", "C:/outside.txt", "/outside.txt", "a/../../outside"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare([{"operation": "create", "path": value, "content": "x"}])
        outside = self.base / "outside.py"
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.root / "link.py").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks are unavailable in this Windows environment")
        with self.assertRaises(ValueError):
            self.prepare([{"operation": "modify", "path": "link.py", "content": "overwrite"}])

    def test_unsaved_conflicts_reject_proposal(self):
        with self.assertRaisesRegex(ValueError, "Unsaved editor changes"):
            coding_changes.prepare(self.root, [{"operation": "modify", "path": "existing.py", "content": "new"}], self.storage, unsaved_paths=["existing.py"])

    def test_unsaved_editor_buffers_block_apply_and_rollback(self):
        proposal=self.prepare([{"operation":"modify","path":"existing.py","content":"approved"}])
        coding_changes.review_all(self.storage,proposal["id"],"approved")
        with self.assertRaisesRegex(ValueError,"Unsaved editor changes"):
            coding_changes.apply(self.storage,proposal["id"],unsaved_paths=["EXISTING.PY"])
        coding_changes.apply(self.storage,proposal["id"])
        with self.assertRaisesRegex(ValueError,"Unsaved editor changes"):
            coding_changes.rollback(self.storage,proposal["id"],unsaved_paths=["existing.py"])
        self.assertEqual((self.root/"existing.py").read_text(encoding="utf-8"),"approved")

    def test_reject_all_revokes_previous_per_file_approval(self):
        proposal=self.prepare([
            {"operation":"modify","path":"existing.py","content":"new"},
            {"operation":"create","path":"other.py","content":"x=1"},
        ])
        coding_changes.review(self.storage,proposal["id"],proposal["changes"][0]["id"],"approved")
        rejected=coding_changes.review_all(self.storage,proposal["id"],"rejected")
        self.assertTrue(all(change["status"]=="rejected" for change in rejected["changes"]))

    def test_rejection_does_not_apply_and_approval_is_recorded(self):
        proposal = self.prepare([{"operation": "modify", "path": "existing.py", "content": "after = 2"}])
        result = coding_changes.review(self.storage, proposal["id"], proposal["changes"][0]["id"], "rejected")
        self.assertEqual(result["changes"][0]["status"], "rejected")
        self.assertTrue(any(event["type"] == "change_rejected" for event in result["events"]))
        with self.assertRaisesRegex(ValueError, "Approve at least one"):
            coding_changes.apply(self.storage, proposal["id"])
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "before = 1\n")

    def test_conflicting_disk_change_is_detected_before_writes(self):
        proposal = self.prepare([{"operation": "modify", "path": "existing.py", "content": "generated"}])
        coding_changes.review_all(self.storage, proposal["id"], "approved")
        (self.root / "existing.py").write_text("user edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Conflict"):
            coding_changes.apply(self.storage, proposal["id"])
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "user edit")

    def test_apply_failure_restores_all_selected_files(self):
        proposal = self.prepare([
            {"operation": "create", "path": "new.txt", "content": "new"},
            {"operation": "modify", "path": "existing.py", "content": "changed"},
        ])
        coding_changes.review_all(self.storage, proposal["id"], "approved")
        original_write = coding_changes._write_text
        calls = 0

        def fail_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            return original_write(path, content)

        with patch.object(coding_changes, "_write_text", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                coding_changes.apply(self.storage, proposal["id"])
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "before = 1\n")
        self.assertEqual(coding_changes.load(self.storage, proposal["id"])["status"], "apply_failed_rolled_back")

    def test_explicit_apply_can_be_rolled_back_with_snapshot_history(self):
        proposal = self.prepare([{"operation": "modify", "path": "existing.py", "content": "after = 2"}])
        coding_changes.review(self.storage, proposal["id"], proposal["changes"][0]["id"], "approved")
        coding_changes.apply(self.storage, proposal["id"])
        before_rollback = []
        rolled = coding_changes.rollback(self.storage, proposal["id"], before_restore=lambda root, path: before_rollback.append(path))
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "before = 1\n")
        self.assertEqual(before_rollback, ["existing.py"])
        self.assertEqual(rolled["status"], "rolled_back")

    def test_rename_and_delete_require_approval_and_restore_on_rollback(self):
        (self.root/"remove.txt").write_text("keep until approved",encoding="utf-8")
        proposal=self.prepare([
            {"operation":"rename","path":"existing.py","new_path":"moved/existing.py","summary":"Move the module"},
            {"operation":"delete","path":"remove.txt","summary":"Remove obsolete file"},
        ])
        self.assertTrue(all(change["status"]=="pending" for change in proposal["changes"]))
        coding_changes.review_all(self.storage,proposal["id"],"approved")
        coding_changes.apply(self.storage,proposal["id"])
        self.assertFalse((self.root/"existing.py").exists())
        self.assertTrue((self.root/"moved"/"existing.py").exists())
        self.assertFalse((self.root/"remove.txt").exists())
        coding_changes.rollback(self.storage,proposal["id"])
        self.assertEqual((self.root/"existing.py").read_bytes(),b"before = 1\r\n")
        self.assertEqual((self.root/"remove.txt").read_text(encoding="utf-8"),"keep until approved")
        self.assertFalse((self.root/"moved").exists())

    def test_interrupted_apply_is_recovered_from_persisted_preimage(self):
        proposal = self.prepare([{"operation": "modify", "path": "existing.py", "content": "after = 2"}])
        coding_changes.review_all(self.storage, proposal["id"], "approved")
        manifest = coding_changes.load(self.storage, proposal["id"])
        manifest["status"] = "applying"
        coding_changes._save(self.storage, manifest)
        (self.root / "existing.py").write_text("after = 2", encoding="utf-8")
        recovered = coding_changes.recover_interrupted(self.storage, self.root)
        self.assertEqual(recovered, [proposal["id"]])
        self.assertEqual((self.root / "existing.py").read_text(encoding="utf-8"), "before = 1\n")
        self.assertEqual(coding_changes.load(self.storage, proposal["id"])["status"], "apply_failed_rolled_back")


if __name__ == "__main__":
    unittest.main()
