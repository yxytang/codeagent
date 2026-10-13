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
            db.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT, created TEXT)")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall USING fts5(title, summary, tokenize='trigram')")
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

    def add(self, scope, title, summary, raw=""):
        with self.connect() as db:
            row = db.execute("INSERT INTO memories(scope,title,summary,raw,created) VALUES(?,?,?,?,?)", (scope, title[:300], summary[:8000], raw[:160000], utcnow()))
            if self.fts:
                db.execute("INSERT INTO recall(rowid,title,summary) VALUES(?,?,?)", (row.lastrowid, title[:300], summary[:8000]))
            return row.lastrowid

    def search(self, query="", scope=None, limit=12, raw=False):
        columns = "m.*" if raw else "m.id,m.scope,m.title,m.summary,m.created"
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

    def save_run(self, run):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs(id,payload,created) VALUES(?,?,?)", (run["id"], json.dumps(run, ensure_ascii=False), run["created"]))

    def load_runs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY created DESC LIMIT 50")]
