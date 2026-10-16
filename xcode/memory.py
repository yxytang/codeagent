from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY, scope TEXT, title TEXT, summary TEXT, raw TEXT, created TEXT)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
            for name, default in (("kind", "legacy"), ("run_id", ""), ("agent", "")):
                if name not in columns:
                    db.execute(f"ALTER TABLE memories ADD COLUMN {name} TEXT DEFAULT '{default}'")
            db.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT, created TEXT)")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall USING fts5(title, summary, tokenize='trigram')")
                # Existing databases may predate FTS support. Preserve their IDs
                # and make every saved memory searchable after the migration.
                db.execute("INSERT INTO recall(rowid,title,summary) SELECT m.id,m.title,m.summary FROM memories m WHERE NOT EXISTS (SELECT 1 FROM recall WHERE rowid=m.id)")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, scope, title, summary, raw="", *, kind="experience", run_id="", agent=""):
        with self.connect() as db:
            row = db.execute("INSERT INTO memories(scope,title,summary,raw,created,kind,run_id,agent) VALUES(?,?,?,?,?,?,?,?)", (scope, title[:300], summary[:8000], raw[:160000], utcnow(), kind, run_id, agent))
            if self.fts:
                db.execute("INSERT INTO recall(rowid,title,summary) VALUES(?,?,?)", (row.lastrowid, title[:300], summary[:8000]))
            return row.lastrowid

    def search(self, query="", scope=None, limit=12, raw=False):
        columns = "m.*" if raw else "m.id,m.scope,m.title,m.summary,m.created,m.kind,m.run_id,m.agent"
        with self.connect() as db:
            args = []
            where = []
            if scope is not None:
                where.append("m.scope=?")
                args.append(scope)
            tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, re.UNICODE)[:16]
            long_tokens = [t for t in tokens if len(t) >= 3]
            for token in tokens:
                if re.fullmatch(r"[\u4e00-\u9fff]{4,}", token):
                    stride = max(1, (len(token) - 2) // 8)
                    long_tokens.extend(token[i:i + 3] for i in range(0, len(token) - 2, stride))
            long_tokens = list(dict.fromkeys(long_tokens))[:24]
            if query and self.fts and long_tokens:
                expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_tokens)
                where.append("recall MATCH ?")
                args.append(expression)
                sql = f"SELECT {columns} FROM memories m JOIN recall ON m.id=recall.rowid WHERE {' AND '.join(where)} ORDER BY recall.rank LIMIT ?"
            elif query:
                where.append("(m.title LIKE ? ESCAPE '\\' OR m.summary LIKE ? ESCAPE '\\')")
                pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                args.extend([pattern, pattern])
                sql = f"SELECT {columns} FROM memories m WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?"
            else:
                sql = f"SELECT {columns} FROM memories m" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.id DESC LIMIT ?"
            return [dict(r) for r in db.execute(sql, [*args, min(max(int(limit), 1), 50)])]

    def get(self, identifier, raw=True):
        columns = "*" if raw else "id,scope,title,summary,created,kind,run_id,agent"
        with self.connect() as db:
            row = db.execute(f"SELECT {columns} FROM memories WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("记忆不存在")
            return dict(row)

    def stats(self):
        with self.connect() as db:
            total, projects = db.execute("SELECT COUNT(*),COUNT(DISTINCT scope) FROM memories").fetchone()
            by_kind = {row[0]: row[1] for row in db.execute("SELECT kind,COUNT(*) FROM memories GROUP BY kind")}
            return {"total": total, "projects": projects, "by_kind": by_kind}

    def project_notes(self, scope, limit=3):
        if not isinstance(scope, str) or not scope:
            raise ValueError("记忆召回必须指定项目")
        limit = min(max(int(limit), 0), 50)
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,scope,title,summary,created,kind,run_id,agent FROM memories WHERE scope=? AND kind='note' ORDER BY id DESC LIMIT ?", (scope, limit))]

    def recall(self, query, scope, limit=4, note_limit=3):
        notes = self.project_notes(scope, note_limit)
        limit = min(max(int(limit), 0), 50)
        if not limit:
            return notes
        seen = {row["id"] for row in notes}
        matches = self.search(query, scope, limit=min(limit + len(notes), 50))
        return notes + [row for row in matches if row["id"] not in seen][:limit]

    def delete_note(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT kind FROM memories WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("记忆不存在")
            if row["kind"] != "note":
                raise ValueError("仅可删除用户项目笔记")
            if self.fts:
                db.execute("DELETE FROM recall WHERE rowid=?", (identifier,))
            db.execute("DELETE FROM memories WHERE id=?", (identifier,))
        return identifier

    def save_run(self, run):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs(id,payload,created) VALUES(?,?,?)", (run["id"], json.dumps(run, ensure_ascii=False), run["created"]))

    def load_runs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY created DESC LIMIT 50")]
