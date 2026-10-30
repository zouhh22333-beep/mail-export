import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True


WORK = Path(__file__).resolve().parent
SCRIPT = WORK.parent / "scripts/archive_store.py"
SPEC = importlib.util.spec_from_file_location("archive_store", SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.store = archive.Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def item(self, **kwargs):
        return {"message_id": "m1", "part_id": "a1", "kind": "attachment",
                "subject": "十一月报价", "original_name": "报价单.pdf", "text": "sample", **kwargs}

    def put(self, item, account="account", check=False):
        return self.store.import_one(account, item, check)

    def test_original_and_explicit_names_preserve_bytes(self):
        source = self.base / "source.bin"
        content = bytes(range(256)) * 5000
        source.write_bytes(content)
        item = self.item(source_path=str(source))
        del item["text"]
        result = self.put(item)
        self.assertEqual(result["state"], "saved")
        self.assertEqual(Path(result["path"]).name, "报价单.pdf")
        self.assertEqual(Path(result["path"]).read_bytes(), content)
        custom = self.put(self.item(part_id="a2", save_name="供应商A.pdf", directory=["采购", "2026"]))
        self.assertEqual(Path(custom["path"]), self.root / "采购/2026/供应商A.pdf")

    def test_never_overwrites_names_case_insensitive(self):
        first = self.put(self.item(original_name="Report.pdf", directory=[]))
        second = self.put(self.item(part_id="a2", original_name="report.pdf", text="new", directory=[]))
        self.assertEqual(Path(second["path"]).name, "report (2).pdf")
        self.assertEqual(Path(first["path"]).read_text(), "sample")
        self.assertEqual(Path(second["path"]).read_text(), "new")

    def test_resume_without_source_and_preflight(self):
        item = self.item()
        first = self.put(item)
        del item["text"]
        result = self.put(item)
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(result["path"], first["path"])
        self.assertEqual(self.put(item, check=True)["state"], "skipped")
        self.assertEqual(self.put(self.item(part_id="a2"), check=True)["state"], "pending")

    def test_indexed_names_retained(self):
        first = self.put(self.item())
        rerun = self.put(self.item(save_name="new.pdf", directory=["elsewhere"]))
        self.assertEqual(rerun["path"], first["path"])
        self.assertIn("retained", rerun["warning"])

    def test_missing_or_changed_file_retries_without_overwrite(self):
        first = self.put(self.item())
        path = Path(first["path"])
        path.unlink()
        self.assertEqual(self.put(self.item(), check=True)["state"], "pending")
        self.assertEqual(self.put(self.item())["state"], "saved")
        path.write_text("edited", encoding="utf-8")
        self.assertEqual(self.put(self.item(), check=True)["state"], "pending")
        again = self.put(self.item())
        self.assertEqual(Path(again["path"]).name, "报价单 (2).pdf")
        self.assertEqual(path.read_text(), "edited")

    def test_failed_source_then_retry_and_other_items(self):
        bad = self.item(source_path=str(self.base / "missing.bin"))
        del bad["text"]
        self.assertEqual(self.put(bad)["state"], "failed")
        self.assertEqual(self.store.status()[0]["state"], "failed")
        self.assertEqual(self.put(self.item())["state"], "saved")
        self.assertEqual(self.put(self.item(part_id="a2"))["state"], "saved")

    def test_traversal_and_reserved_index_directory(self):
        for parts in ([".."], ["a/b"], ["C:\\temp"], [".mail-export"]):
            with self.subTest(parts=parts):
                self.assertEqual(self.put(self.item(directory=parts))["state"], "failed")
        result = self.put(self.item(original_name="../../escape.pdf", directory=[]))
        self.assertEqual(Path(result["path"]).parent, self.root)
        self.assertFalse((self.base / "escape.pdf").exists())

    def test_symlink_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"OS disallows symlink creation: {exc.__class__.__name__}")
            # Directory junctions exercise the same resolve/containment boundary
            # on Windows without requiring the symbolic-link privilege.
            command = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null"
            result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True)
            if result.returncode:
                self.skipTest("OS disallows both symbolic links and directory junctions")
        self.assertEqual(self.put(self.item(directory=["escape"]))["state"], "failed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_default_directories_isolate_same_subject(self):
        a = self.put(self.item())
        b = self.put(self.item(message_id="m2"))
        c = self.put(self.item(), account="another")
        self.assertEqual(len({Path(r["path"]).parent for r in (a, b, c)}), 3)

    def test_sanitation_reserved_long_unicode(self):
        for bad, expected in [("CON.txt", "_CON.txt"), ("a:b?.pdf", "a_b_.pdf"), ("x. ", "x")]:
            self.assertEqual(archive.safe_name(bad), expected)
        name = archive.safe_name("文档😀" * 80 + ".pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertLessEqual(len(name.encode("utf-16-le")) // 2, 240)
        self.assertLessEqual(len(name.encode("utf-8")), 240)
        self.assertEqual(archive.safe_name("a" * 120 + ".pdf"), "a" * 120 + ".pdf")
        result = self.put(self.item(original_name=name))
        self.assertEqual(result["state"], "saved")

    def test_email_fallbacks_and_non_eml_unknown_format(self):
        text = self.put(self.item(kind="email", original_name=None))
        self.assertTrue(text["path"].endswith("十一月报价.txt"))
        source = self.base / "mail.eml"
        source.write_bytes(b"Subject: Original\r\n\r\ncontent")
        item = self.item(kind="email", part_id="raw", original_name=None, source_path=str(source))
        del item["text"]
        eml = self.put(item)
        self.assertEqual(Path(eml["path"]).suffix, ".eml")
        source = self.base / "mail.bin"
        source.write_bytes(b"x")
        item.update(part_id="unknown", source_path=str(source))
        self.assertEqual(self.put(item)["state"], "failed")

    def test_connector_failure_redaction(self):
        item = self.item(error="Bearer TOP_SECRET and private mail body")
        result = self.put(item)
        self.assertEqual(result["error"], "connector_error")
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_missing_attachment_names_and_fallback_priority(self):
        missing = self.put(self.item(original_name=None))
        self.assertEqual(Path(missing["path"]).name, "附件_a1")
        self.assertIsNone(self.store.row(("account", "m1", "a1"))["original_name"])
        fallback = self.put(self.item(part_id="a2", original_name="", fallback_name="附件_a2.pdf"))
        self.assertEqual(Path(fallback["path"]).name, "附件_a2.pdf")
        self.assertEqual(self.store.row(("account", "m1", "a2"))["original_name"], "")
        original = self.put(self.item(part_id="a3", original_name="原始.pdf", fallback_name="fallback.pdf"))
        self.assertEqual(Path(original["path"]).name, "原始.pdf")
        explicit = self.put(self.item(part_id="a4", original_name="原始.pdf", save_name="用户.pdf", fallback_name="fallback.pdf"))
        self.assertEqual(Path(explicit["path"]).name, "用户.pdf")

    def test_invalid_metadata_does_not_abort_batch(self):
        self.assertEqual(self.put(self.item(subject={"oops": 1}))["state"], "failed")
        self.assertEqual(self.put(self.item(part_id="other", original_name={}))["state"], "failed")
        self.assertEqual(self.put(self.item(part_id="good"))["state"], "saved")

    def test_published_file_recovery(self):
        saved = self.put(self.item())
        self.store.db.execute("UPDATE artifacts SET state='publishing'")
        self.store.db.commit()
        recovered = self.put({"message_id": "m1", "part_id": "a1", "kind": "attachment"})
        self.assertEqual(recovered["state"], "skipped")
        self.assertEqual(recovered["path"], saved["path"])
        self.assertEqual(self.store.row(("account", "m1", "a1"))["state"], "saved")

    def test_lock_and_cli_partial_failure_exit_codes(self):
        with self.assertRaises(archive.ArchiveError):
            archive.Store(self.root)
        manifest = self.base / "batch.json"
        manifest.write_text(json.dumps({"version": 1, "account_id": "account", "items": [self.item(), self.item(part_id="bad", error="offline")]}), encoding="utf-8")
        self.store.close()
        command = [sys.executable, str(SCRIPT), "import", "--root", str(self.root), "--manifest", str(manifest)]
        result = subprocess.run(command, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["saved"], 1)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["unit"], "artifacts")
        status = subprocess.run([sys.executable, str(SCRIPT), "status", "--root", str(self.root)], capture_output=True, encoding="utf-8")
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)["counts"]["failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

