import json
import tempfile
import unittest

import cursor_sync


class TestNormalizeAndCount(unittest.TestCase):
    def test_normalize_collapses_numbers_and_case(self):
        first = cursor_sync.normalize_message("Agent ONLINE 123")
        second = cursor_sync.normalize_message("agent online 456")
        self.assertEqual(first, second)
        self.assertIn("<N>", first)

    def test_count_templates(self):
        messages = ["agent online", "agent online", "vps ok 123"]
        counts = cursor_sync.count_templates(messages)
        self.assertEqual(counts.get("agent online"), 2)
        self.assertEqual(sum(counts.values()), 3)

    def test_find_noisy_and_summary(self):
        messages = ["agent online"] * 5 + ["vps ok"] * 1
        noisy = cursor_sync.find_noisy_templates(messages, threshold=3)
        self.assertEqual(len(noisy), 1)
        self.assertEqual(noisy[0][1], 5)
        summary = cursor_sync.summarize_log(messages, threshold=3)
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["unique_templates"], 2)
        self.assertGreater(summary["noise_ratio"], 0.5)


class TestTelemetryParser(unittest.TestCase):
    def test_valid_seconds_percent(self):
        parsed = cursor_sync.parse_telemetry(
            "contribution v1 uptime 3600s ram 42% load 0.85"
        )
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["uptime_seconds"], 3600)
        self.assertEqual(parsed["ram_percent"], 42.0)
        self.assertEqual(parsed["load_1m"], 0.85)

    def test_valid_hours_megabytes(self):
        parsed = cursor_sync.parse_telemetry("uptime: 2h ram: 2048MB load: 1.5")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["uptime_seconds"], 7200)
        self.assertEqual(parsed["ram_mb"], 2048.0)
        self.assertIsNone(parsed["ram_percent"])

    def test_valid_gigabytes(self):
        parsed = cursor_sync.parse_telemetry("uptime 1d ram 2GB load 0.2")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["uptime_seconds"], 86400)
        self.assertEqual(parsed["ram_mb"], 2048.0)

    def test_invalid_missing_fields(self):
        parsed = cursor_sync.parse_telemetry("hello world")
        self.assertFalse(parsed["valid"])
        self.assertTrue(len(parsed["errors"]) > 0)

    def test_invalid_empty(self):
        parsed = cursor_sync.parse_telemetry("   ")
        self.assertFalse(parsed["valid"])


class TestDidChecker(unittest.TestCase):
    def test_valid_did(self):
        result = cursor_sync.check_did_format("did:key:z6MkpTHR8VNsBxYAA")
        self.assertTrue(result["valid"])
        self.assertEqual(result["method"], "key")

    def test_invalid_prefix(self):
        result = cursor_sync.check_did_format("hello")
        self.assertFalse(result["valid"])

    def test_invalid_method_case(self):
        result = cursor_sync.check_did_format("did:KEY:z6MkpTHR8VNsBxYAA")
        self.assertFalse(result["valid"])

    def test_invalid_short_identifier(self):
        result = cursor_sync.check_did_format("did:key:abc")
        self.assertFalse(result["valid"])


class TestManifest(unittest.TestCase):
    def test_build_verify_roundtrip(self):
        files = {"report.json": '{"total": 3}', "notes.txt": "hello"}
        manifest = cursor_sync.build_manifest(files)
        self.assertIn("report.json", manifest["files"])
        check = cursor_sync.verify_manifest(manifest, files)
        self.assertTrue(check["ok"])

    def test_verify_detects_change(self):
        files = {"a.txt": "one"}
        manifest = cursor_sync.build_manifest(files)
        bad = {"a.txt": "two"}
        check = cursor_sync.verify_manifest(manifest, bad)
        self.assertFalse(check["ok"])
        self.assertEqual(check["mismatched"], ["a.txt"])

    def test_write_and_read_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/manifest.json"
            files = {"a.txt": "data123"}
            manifest = cursor_sync.build_manifest(files)
            cursor_sync.write_manifest_file(manifest, path)
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded, manifest)
            check = cursor_sync.verify_manifest(loaded, files)
            self.assertTrue(check["ok"])

    def test_sha256_known(self):
        digest = cursor_sync.sha256_hex("abc")
        self.assertEqual(digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class TestCursorPoller(unittest.TestCase):
    def test_poll_once_advances(self):
        log = [
            {"seq": 1, "text": "agent online"},
            {"seq": 2, "text": "uptime 10s ram 10% load 0.1"},
        ]

        def fetch(after_seq, wait):
            batch = [m for m in log if m["seq"] > after_seq]
            nxt = max([m["seq"] for m in batch], default=after_seq)
            return (batch, nxt)

        with tempfile.TemporaryDirectory() as tmp:
            point = tmp + "/cursor.json"
            poller = cursor_sync.CursorPoller(fetch, checkpoint_path=point, wait_seconds=10)
            batch = poller.poll_once()
            self.assertEqual(len(batch), 2)
            self.assertEqual(poller.cursor, 2)
            with open(point, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["seq"], 2)

    def test_checkpoint_resume(self):
        log = [
            {"seq": 1, "text": "one"},
            {"seq": 2, "text": "two"},
            {"seq": 3, "text": "three"},
        ]

        def fetch(after_seq, wait):
            batch = [m for m in log if m["seq"] > after_seq][:1]
            if batch:
                nxt = batch[-1]["seq"]
            else:
                nxt = after_seq
            return {"messages": batch, "next_seq": nxt}

        with tempfile.TemporaryDirectory() as tmp:
            point = tmp + "/cursor.json"
            first = cursor_sync.CursorPoller(fetch, checkpoint_path=point, start_seq=0)
            got_one = first.poll_once()
            self.assertEqual(len(got_one), 1)
            self.assertEqual(first.cursor, 1)
            second = cursor_sync.CursorPoller(fetch, checkpoint_path=point, start_seq=0)
            self.assertEqual(second.cursor, 1)
            rest = second.poll_many(max_batches=5, pause_seconds=0.0)
            self.assertEqual(len(rest), 2)
            self.assertEqual(second.cursor, 3)

    def test_wait_clamped(self):
        def fetch(after_seq, wait):
            self.assertGreaterEqual(wait, 1)
            self.assertLessEqual(wait, 60)
            return ([], after_seq)

        poller = cursor_sync.CursorPoller(fetch, wait_seconds=999)
        self.assertEqual(poller.wait_seconds, 60)
        poller.poll_once()


if __name__ == "__main__":
    unittest.main()
