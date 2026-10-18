import os
import re
import time
import math
import hashlib
import sqlite3
import struct
from pathlib import Path
import sqlite_vec
from sentence_transformers import SentenceTransformer, CrossEncoder
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "~/MemoryVault")).expanduser().resolve()
DB_PATH = Path("~/.local/share/memory-vault/index.sqlite").expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

embedder = SentenceTransformer(EMBED_MODEL_NAME)
reranker = CrossEncoder(RERANK_MODEL_NAME)

def serialize_f32(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                heading TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                mtime REAL NOT NULL,
                UNIQUE(file_path, heading)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                heading, content, content='chunks', content_rowid='chunk_id'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding FLOAT[384]
            );
        """)

def chunk_markdown(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(?=##\s+)", text)
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        heading = lines[0].lstrip("#").strip() if lines[0].startswith("#") else "Root"
        chunks.append((heading, part))
    return chunks

def index_file(md_path: Path):
    if not md_path.exists() or md_path.suffix != ".md":
        return
    rel_path = str(md_path.relative_to(VAULT_DIR))
    mtime = md_path.stat().st_mtime
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_markdown(text)

    with get_db() as conn:
        existing = {
            row["heading"]: (row["chunk_id"], row["content_hash"])
            for row in conn.execute(
                "SELECT chunk_id, heading, content_hash FROM chunks WHERE file_path = ?",
                (rel_path,)
            )
        }
        seen_headings = set()

        for heading, content in chunks:
            seen_headings.add(heading)
            c_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if heading in existing and existing[heading][1] == c_hash:
                continue

            vec = embedder.encode(content, normalize_embeddings=True).tolist()
            if heading in existing:
                cid = existing[heading][0]
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (cid,))
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (cid,))
                conn.execute(
                    "UPDATE chunks SET content=?, content_hash=?, mtime=? WHERE chunk_id=?",
                    (content, c_hash, mtime, cid)
                )
            else:
                cur = conn.execute(
                    "INSERT INTO chunks (file_path, heading, content, content_hash, mtime) VALUES (?, ?, ?, ?, ?)",
                    (rel_path, heading, content, c_hash, mtime)
                )
                cid = cur.lastrowid

            conn.execute(
                "INSERT INTO chunks_fts(rowid, heading, content) VALUES (?, ?, ?)",
                (cid, heading, content)
            )
            conn.execute(
                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                (cid, serialize_f32(vec))
            )

        for old_heading, (old_cid, _) in existing.items():
            if old_heading not in seen_headings:
                conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (old_cid,))
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (old_cid,))
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (old_cid,))

def hybrid_search(query: str, top_k: int = 5, rrf_k: int = 60) -> list[dict]:
    q_vec = serialize_f32(embedder.encode(query, normalize_embeddings=True).tolist())
    safe_fts_query = " OR ".join(re.findall(r"\w+", query))

    with get_db() as conn:
        vec_rows = conn.execute("""
            SELECT chunk_id, distance FROM chunks_vec
            WHERE embedding MATCH ? AND k = 25 ORDER BY distance
        """, (q_vec,)).fetchall()

        fts_rows = conn.execute("""
            SELECT rowid AS chunk_id, bm25(chunks_fts) AS score FROM chunks_fts
            WHERE chunks_fts MATCH ? ORDER BY score LIMIT 25
        """, (safe_fts_query,)).fetchall() if safe_fts_query else []

        rrf_scores: dict[int, float] = {}
        for rank, row in enumerate(vec_rows):
            cid = row["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, row in enumerate(fts_rows):
            cid = row["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)

        if not rrf_scores:
            return []

        top_cids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:25]
        placeholders = ",".join("?" for _ in top_cids)
        candidates = conn.execute(
            f"SELECT chunk_id, file_path, heading, content, mtime FROM chunks WHERE chunk_id IN ({placeholders})",
            top_cids
        ).fetchall()

    now = time.time()
    pairs = [[query, f"{c['file_path']} | {c['heading']}\n{c['content']}"] for c in candidates]
    ce_scores = reranker.predict(pairs)

    scored = []
    for c, ce_score in zip(candidates, ce_scores):
        age_days = max(0.0, (now - c["mtime"]) / 86400.0)
        recency_boost = 1.0 if c["file_path"].startswith("canonical/") else math.exp(-0.0115 * age_days)
        final_score = float(ce_score) + (0.5 * recency_boost)
        scored.append({
            "file_path": c["file_path"],
            "heading": c["heading"],
            "content": c["content"],
            "score": round(final_score, 4)
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]

class VaultWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            index_file(Path(event.src_path))
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            index_file(Path(event.src_path))

if __name__ == "__main__":
    init_db()
    for md in sorted(VAULT_DIR.rglob("*.md")):
        index_file(md)
    print(f"[INDEXER] Indexed all Markdown files in {VAULT_DIR}")
