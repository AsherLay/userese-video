import copy
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from userese_video.cli import main
from userese_video.ingest import create, demo, parse_transcript
from userese_video.media import build, fingerprint, history, preview, probe, run
from userese_video.model import Conflict, Project, relative_file, write_json
from userese_video.server import Workbench


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="userese-tests-")
        cls.original = demo(Path(cls.fixture.name) / "fixture")

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="userese-case-")
        self.root = Path(self.temporary.name) / "project"
        shutil.copytree(self.original.root, self.root)
        self.project = Project(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def confirmed(self):
        state = self.project.state()
        for choice in state["choices"].values():
            choice["status"] = "keep"
        return self.project.save(state, state["revision"])

    def test_import_refuses_to_overwrite_existing_work(self):
        before = fingerprint(self.root / "decisions.json")
        with self.assertRaises(ValueError):
            create(self.root, "replace", [])
        self.assertEqual(before, fingerprint(self.root / "decisions.json"))

    def test_saved_decisions_survive_restart_and_conflicts(self):
        state = self.project.state()
        state["choices"]["opening"].update(status="keep", take="opening-a", note="保留停顿")
        saved = self.project.save(state, 0)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(Project(self.root).state()["choices"]["opening"]["note"], "保留停顿")
        with self.assertRaises(Conflict):
            self.project.save(state, 0)
        self.assertTrue((self.root / "history" / "decisions-000000.json").is_file())

    def test_snapshot_roundtrip_and_cross_project_rejection(self):
        original = self.project.state()
        modified = copy.deepcopy(original)
        modified["captions"]["source-001-cue-0002"] = "新的正确字幕"
        self.project.save(modified, 0)
        restored = self.project.save(original, 1)
        self.assertEqual(restored["captions"], {})
        wrong = copy.deepcopy(restored)
        wrong["project_id"] = "another-project"
        with self.assertRaises(ValueError):
            self.project.save(wrong, 2)

    def test_unconfirmed_and_optimization_require_explicit_decisions(self):
        state = self.project.state()
        with self.assertRaises(ValueError):
            self.project.selected(state)
        self.assertEqual(len(self.project.selected(state, draft=True)), 3)
        state["choices"]["body"]["status"] = "optimize"
        with self.assertRaises(ValueError):
            self.project.selected(state, draft=True)

    def test_invalid_choices_and_caption_markup_rejected(self):
        for mutate in (
            lambda state: state["choices"]["opening"].update(take="body-a"),
            lambda state: state["choices"]["opening"].update(status="skip", take="opening-a"),
            lambda state: state["captions"].update({"source-001-cue-0001": "<script>bad</script>"}),
            lambda state: state["captions"].update({"unknown": "字幕"}),
        ):
            state = self.project.state()
            mutate(state)
            with self.assertRaises(ValueError):
                self.project.save(state, 0)

    def test_proposals_preserve_human_decisions_and_ids(self):
        self.confirmed()
        before = self.project.state()
        result = main(["propose", str(self.root), "--family", "opening", "--id", "opening-combined", "--label", "组合建议",
                       "--part", "source-001", "0", "2", "--part", "source-001", "2", "4", "--revision", "1"])
        self.assertEqual(result, 0)
        state = self.project.state()
        self.assertEqual(state["choices"], before["choices"])
        self.assertIn("opening-combined", self.project.all_takes(state))
        state["proposals"]["opening"][0]["label"] = "改写旧编号"
        with self.assertRaises(ValueError):
            self.project.save(state, 2)
        restored = self.project.save(before, 2)
        self.assertIn("opening-combined", self.project.all_takes(restored))

    def test_cuts_cannot_truncate_a_caption_or_use_nonfinite_time(self):
        state = self.project.state()
        for start in (1, float("nan")):
            state["proposals"] = {"opening": [{"id": "bad-cut", "label": "断句", "parts": [{"source": "source-001", "start": start, "end": 2}]}]}
            with self.assertRaises(ValueError):
                self.project.save(state, 0)

    def test_import_srt_multisource_and_silent_footage(self):
        temp = Path(self.temporary.name)
        silent = temp / "silent.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-i", str(self.root / "media/source-001.mp4"), "-t", "2", "-an", "-c:v", "copy", str(silent)])
        srt = temp / "words.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nA complete sentence.\n", encoding="utf-8")
        result = create(temp / "second-project", "另一个作者的项目", [(silent, srt), (silent, srt)], output={"width": 240, "height": 426})
        self.assertEqual(len(result.sources), 2)
        self.assertFalse(result.sources["source-001"]["audio"])
        manifest = build(result, draft=True, burn=False)
        self.assertEqual(manifest["duration"], 4)
        streams = probe(result.root / "builds" / manifest["id"] / "video.mp4")["streams"]
        self.assertTrue(any(stream["codec_type"] == "audio" for stream in streams))

    def test_measured_clock_across_fractional_cuts(self):
        temp = Path(self.temporary.name)
        transcript = temp / "fractional.json"
        write_json(transcript, [{"start": i * .37, "end": i * .37 + .33, "text": f"Sentence {i}"} for i in range(12)])
        result = create(temp / "clock-project", "Fractional clock", [(self.root / "media/source-001.mp4", transcript)], output={"width": 160, "height": 284})
        manifest = build(result, draft=True, burn=False)
        self.assertAlmostEqual(manifest["duration"], 12 * .36, places=2)
        self.assertAlmostEqual(manifest["blocks"][-1]["output_start"], 11 * .36, places=4)
        self.assertLessEqual(manifest["captions"][-1]["end"], manifest["duration"])

    def test_real_rebuild_applies_corrections_and_retains_old_version(self):
        original_source = fingerprint(self.root / "media/source-001.mp4")
        first = build(self.project, draft=True)
        first_video = self.root / "builds" / first["id"] / "video.mp4"
        first_hash = fingerprint(first_video)
        state = self.confirmed()
        state["choices"]["opening"]["take"] = "opening-a"
        state["captions"]["source-001-cue-0001"] = "人工纠正后的字幕"
        state["choices"]["body"].update(status="skip", take=None)
        saved = self.project.save(state, state["revision"])
        second = build(self.project)
        self.assertEqual(second["revision"], saved["revision"])
        self.assertEqual(second["duration"], 6)
        self.assertEqual(second["blocks"][0]["take"], "opening-a")
        captions = (self.root / "builds" / second["id"] / "captions.srt").read_text(encoding="utf-8")
        self.assertIn("人工纠正后的字幕", captions)
        self.assertNotIn("比较候选", captions)
        self.assertEqual(first_hash, fingerprint(first_video))
        self.assertNotEqual(first_hash, second["sha256"])
        self.assertEqual(original_source, fingerprint(self.root / "media/source-001.mp4"))
        self.assertEqual(len(history(self.project)), 2)
        self.assertTrue((self.root / "builds" / second["id"] / "cover.jpg").is_file())

    def test_source_mutation_stops_build(self):
        with (self.root / "media/source-001.mp4").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            build(self.project, draft=True)

    def test_context_preview_contains_neighbors(self):
        key = preview(self.project, "body-a", context=True)
        info = probe(self.root / "cache" / key / "preview.mp4")
        self.assertAlmostEqual(float(info["format"]["duration"]), 9, delta=.1)
        self.assertEqual(key, preview(self.project, "body-a", context=True))

    def test_relative_paths_block_parent_and_symlink_escape(self):
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.root / "escape.txt").symlink_to(outside)
        for path in ("../outside.txt", "escape.txt", str(outside)):
            with self.assertRaises(ValueError):
                relative_file(self.root, path)

    def test_invalid_transcript_leaves_no_partial_project(self):
        temp = Path(self.temporary.name)
        transcript = temp / "bad.json"
        write_json(transcript, [{"start": 0, "end": 30, "text": "Out of bounds"}])
        with self.assertRaises(ValueError):
            create(temp / "invalid", "Bad input", [(self.root / "media/source-001.mp4", transcript)])
        self.assertFalse((temp / "invalid").exists())
        self.assertEqual(list(temp.glob(".userese-import-*")), [])

    def test_http_auth_origin_ranges_conflicts_and_paths(self):
        server = Workbench(("127.0.0.1", 0), self.project)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        session_header = ""

        def request(path, body=None, headers=None, method=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            values = {"Cookie": session_header, **(headers or {})}
            if body is not None:
                values["Content-Type"] = "application/json"
            connection.request(method or ("POST" if body is not None else "GET"), path,
                               json.dumps(body) if body is not None else None, values)
            response = connection.getresponse()
            result = (response.status, dict(response.getheaders()), response.read())
            connection.close()
            return result

        try:
            self.assertEqual(request("/")[0], 200)
            self.assertEqual(request("/api/project")[0], 401)
            self.assertEqual(request("/media/source/source-001")[0], 401)
            self.assertEqual(request("/api/session", {"token": "wrong"})[0], 401)
            status, headers, _ = request("/api/session", {"token": server.token})
            self.assertEqual(status, 200)
            session_header = headers["Set-Cookie"].split(";")[0]
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            self.assertEqual(request("/api/project")[0], 200)
            self.assertEqual(request("/api/project", headers={"Host": "attacker.invalid"})[0], 403)
            self.assertEqual(request("/api/state", {}, {"Origin": "https://attacker.invalid"})[0], 403)
            self.assertEqual(request("/api/state", [], method="POST")[0], 400)
            self.assertEqual(request("/%2e%2e/project.json")[0], 404)
            status, headers, data = request("/media/source/source-001", headers={"Range": "bytes=0-31"})
            self.assertEqual(status, 206)
            self.assertEqual(len(data), 32)
            self.assertTrue(headers["Content-Range"].startswith("bytes 0-31/"))
            self.assertEqual(request("/media/source/source-001", headers={"Range": "bytes=999999999-"})[0], 416)
            self.assertEqual(request("/media/source/source-001", method="HEAD")[2], b"")
            payload = {"state": self.project.state(), "expected_revision": 0}
            self.assertEqual(request("/api/state", payload)[0], 200)
            self.assertEqual(request("/api/state", payload)[0], 409)
            self.assertEqual(json.loads(request("/api/export")[2])["revision"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
