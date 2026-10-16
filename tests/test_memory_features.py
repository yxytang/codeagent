from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from xcode.config import Settings
from xcode.engine import Engine
from xcode.memory import MemoryStore
from xcode.server import make_handler
from xcode.skills import SkillRegistry


class MemoryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_old_database_migrates_in_place_and_backfills_fts(self):
        path = self.root / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, scope TEXT, title TEXT, summary TEXT, raw TEXT, created TEXT)")
            db.execute("INSERT INTO memories VALUES(7,'project','legacy keyword','old summary','original transcript','2026-01-01')")
        db.close()
        memory = MemoryStore(path)
        row = memory.get(7)
        self.assertEqual((row["kind"], row["run_id"], row["agent"]), ("legacy", "", ""))
        self.assertEqual(row["raw"], "original transcript")
        self.assertEqual(memory.search("keyword", "project")[0]["id"], 7)
        identifier = memory.add("project", "new keyword", "new summary", "raw", kind="experience", run_id="run-1", agent="writer")
        public = memory.search("new", "project")[0]
        self.assertEqual((public["kind"], public["run_id"], public["agent"]), ("experience", "run-1", "writer"))
        self.assertNotIn("raw", public)
        # Reopening is idempotent and neither duplicates nor loses FTS rows.
        memory = MemoryStore(path)
        self.assertEqual(memory.stats(), {"total": 2, "projects": 1, "by_kind": {"legacy": 1, "experience": 1}})
        if memory.fts:
            with memory.connect() as db:
                self.assertEqual([r[0] for r in db.execute("SELECT rowid FROM recall ORDER BY rowid")], [7, identifier])

    def test_notes_are_recalled_without_keyword_and_never_cross_projects(self):
        memory = MemoryStore(self.root / "memory.sqlite3")
        old = memory.add("project-a", "old convention", "Use type hints", kind="note", agent="user")
        experience = memory.add("project-a", "database migration", "Keep SQLite compatible", run_id="run-1")
        newest = memory.add("project-a", "new convention", "Use unittest", kind="note", agent="user")
        foreign = memory.add("project-b", "database migration", "Unrelated conventions", kind="note")
        rows = memory.recall("database", "project-a", limit=1, note_limit=2)
        self.assertEqual([r["id"] for r in rows], [newest, old, experience])
        self.assertNotIn(foreign, [r["id"] for r in rows])
        self.assertEqual(memory.recall("does-not-exist", "project-b")[0]["id"], foreign)
        self.assertEqual(memory.recall("database", "project-c"), [])
        with self.assertRaises(ValueError):
            memory.recall("database", None)
        self.assertEqual([r["id"] for r in memory.recall("convention", "project-a", limit=4, note_limit=2)], [newest, old])

    def test_only_notes_can_be_deleted_and_fts_stays_consistent(self):
        memory = MemoryStore(self.root / "memory.sqlite3")
        note = memory.add("p", "unique delete marker", "note", kind="note")
        experience = memory.add("p", "preserved record", "experience")
        with self.assertRaises(ValueError):
            memory.delete_note(experience)
        self.assertEqual(memory.delete_note(note), note)
        self.assertEqual(memory.search("unique delete marker", "p"), [])
        self.assertEqual(memory.stats()["total"], 1)
        self.assertEqual(memory.get(experience)["kind"], "experience")
        with self.assertRaises(ValueError):
            memory.delete_note(note)
        with self.assertRaises(ValueError):
            memory.get(note)
        if memory.fts:
            with memory.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM recall WHERE rowid=?", (note,)).fetchone()[0], 0)

    def test_existing_storage_length_bounds_are_preserved(self):
        memory = MemoryStore(self.root / "memory.sqlite3")
        identifier = memory.add("p", "t" * 301, "s" * 8001, "r" * 160001)
        row = memory.get(identifier)
        self.assertEqual((len(row["title"]), len(row["summary"]), len(row["raw"])), (300, 8000, 160000))

    def test_disabled_skill_can_be_previewed_but_not_loaded(self):
        folder = self.root / "skills" / "quality"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: quality\ndescription: Python checks\ntools: [read_file]\n---\nRun useful checks.", encoding="utf-8")
        registry = SkillRegistry([folder.parent], disabled=["quality"])
        preview = registry.preview("quality")
        self.assertEqual(set(preview), {"name", "description", "tools", "enabled", "body", "path"})
        self.assertFalse(preview["enabled"])
        self.assertEqual(preview["body"], "Run useful checks.")
        with self.assertRaises(ValueError):
            registry.load("quality")
        with self.assertRaises(ValueError):
            registry.preview("missing")


class MemoryFeatureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / ".xcode")
        self.engine = Engine(self.root / ".xcode", self.root, self.settings)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, self.settings))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, route, data=None, headers=None):
        common = {"Content-Type": "application/json", "X-XCode-Client": "1"}
        common.update(headers or {})
        request = urllib.request.Request(self.base + route, data=json.dumps(data).encode() if data is not None else None, headers=common)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_create_list_stats_and_delete_note(self):
        status, result = self.request("/api/memory", {"scope": str(self.root / "."), "title": "项目约定", "summary": "使用 unittest"})
        self.assertEqual(status, 201)
        row = result["memory"]
        self.assertEqual((row["scope"], row["kind"], row["agent"]), (str(self.root.resolve()), "note", "user"))
        _, state = self.request("/api/state")
        self.assertEqual(state["memory_stats"], {"total": 1, "projects": 1, "by_kind": {"note": 1}})
        _, rows = self.request("/api/memory?" + urllib.parse.urlencode({"scope": str(self.root.resolve())}))
        self.assertEqual(rows[0]["id"], row["id"])
        self.assertNotIn("raw", rows[0])
        _, deleted = self.request(f"/api/memory/{row['id']}/delete", {})
        self.assertEqual(deleted, {"deleted": row["id"]})
        self.assertEqual(self.engine.memory.stats()["total"], 0)

    def test_note_validation_rejects_bad_paths_types_and_lengths(self):
        valid = {"scope": str(self.root), "title": "title", "summary": "summary"}
        file = self.root / "file.txt"
        file.write_text("original", encoding="utf-8")
        invalid = [{"scope": "relative"}, {"scope": str(file)}, {"scope": str(self.root / "missing")}, {"scope": None}, {"title": " "}, {"title": "x" * 301}, {"title": 12}, {"summary": ""}, {"summary": "x" * 8001}, {"summary": []}]
        for update in invalid:
            with self.subTest(update={key: type(value).__name__ for key, value in update.items()}), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/memory", valid | update)
            self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.engine.memory.stats()["total"], 0)
        self.assertEqual(file.read_text(encoding="utf-8"), "original")

    def test_memory_detail_finds_exact_old_record_beyond_search_limit(self):
        identifiers = [self.engine.memory.add(str(self.root), "Repeated context title", f"summary-{i}", f"raw-{i}", kind="context", run_id=f"run-{i}", agent="writer") for i in range(16)]
        _, recent = self.request("/api/memory?" + urllib.parse.urlencode({"scope": str(self.root)}))
        self.assertEqual(len(recent), 12)
        self.assertNotIn(identifiers[0], [row["id"] for row in recent])
        _, detail = self.request(f"/api/memory/{identifiers[0]}")
        self.assertEqual((detail["id"], detail["summary"], detail["run_id"]), (identifiers[0], "summary-0", "run-0"))
        self.assertNotIn("raw", detail)
        _, raw = self.request(f"/api/memory/{identifiers[0]}?raw=1")
        self.assertEqual(raw["raw"], "raw-0")
        self.assertEqual(raw["scope"], str(self.root))

    def test_memory_detail_rejects_invalid_and_missing_ids(self):
        for identifier in ("no-id", "0", "-1", "9223372036854775808", "999999", "1/extra"):
            with self.subTest(identifier=identifier), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(f"/api/memory/{identifier}")
            self.assertEqual(raised.exception.code, 400)
            with self.subTest(delete_identifier=identifier), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(f"/api/memory/{identifier}/delete", {})
            self.assertEqual(raised.exception.code, 400)

    def test_note_mutations_require_trusted_source_and_preserve_experience(self):
        data = {"scope": str(self.root), "title": "title", "summary": "summary"}
        for headers in ({"X-XCode-Client": ""}, {"Origin": "https://foreign.example"}, {"Host": "foreign.example"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/memory", data, headers)
            self.assertEqual(raised.exception.code, 403)
        identifier = self.engine.memory.add(str(self.root), "generated", "experience")
        for path in (f"/api/memory/{identifier}/delete", "/api/memory/999999/delete", "/api/memory/no-id/delete"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(path, {})
            self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.engine.memory.stats()["total"], 1)

    def test_http_disabled_skill_preview(self):
        folder = self.root / "skills" / "quality"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: quality\ndescription: checks\n---\nReadable body", encoding="utf-8")
        self.settings.update({"disabled_skills": ["quality"]})
        _, preview = self.request("/api/skills?name=qual%69ty")
        self.assertEqual(preview["body"], "Readable body")
        self.assertFalse(preview["enabled"])
        with self.assertRaises(ValueError):
            self.engine.skills().load("quality")


if __name__ == "__main__":
    unittest.main()
