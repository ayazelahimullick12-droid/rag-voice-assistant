"""
Knowledge base + retriever for the BRAC Bengali Voice Assistant.

Reads every .txt / .md file in the `knowledge/` folder, splits them into
overlapping chunks, and scores them against a query using:
  * semantic similarity (Gemini embedding model, cached)
  * lexical similarity (tf-idf over words + character trigrams)
"""

import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import storage

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "gemini-embedding-001")
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.15"))

CHUNK_TARGET_CHARS = 900
CHUNK_OVERLAP_CHARS = 180
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown"}

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")


def _cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    if dot == 0.0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _cosine_dense(a: List[float], b: List[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _words(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


def _trigrams(text: str) -> List[str]:
    flat = re.sub(r"\s+", " ", text.lower()).strip()
    return [flat[i:i + 3] for i in range(max(0, len(flat) - 2))]


def _split_into_sections(text: str) -> List[Tuple[str, str]]:
    """Split markdown/plain text into (heading, body) sections."""
    sections: List[Tuple[str, List[str]]] = []
    heading = ""
    buf: List[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if buf and any(l.strip() for l in buf):
                sections.append((heading, buf))
            heading = m.group(2).strip()
            buf = []
        else:
            buf.append(line)
    if buf and any(l.strip() for l in buf):
        sections.append((heading, buf))
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip()]


def _pack_paragraphs(heading: str, body: str) -> List[str]:
    """Pack paragraphs into ~CHUNK_TARGET_CHARS chunks."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    prefix = f"{heading}\n" if heading else ""
    chunks: List[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append((prefix + current.strip()).strip())
        current = ""

    for para in paras:
        if len(para) > CHUNK_TARGET_CHARS * 1.6:
            flush()
            step = CHUNK_TARGET_CHARS - CHUNK_OVERLAP_CHARS
            for i in range(0, len(para), step):
                piece = para[i:i + CHUNK_TARGET_CHARS]
                if piece.strip():
                    chunks.append((prefix + piece.strip()).strip())
            continue
        if len(current) + len(para) + 2 > CHUNK_TARGET_CHARS and current:
            flush()
        current += ("\n\n" if current else "") + para
    flush()
    return chunks


class Chunk:
    __slots__ = ("id", "source", "heading", "text", "words", "trigrams",
                 "wvec", "tvec", "embedding")

    def __init__(self, cid: int, source: str, heading: str, text: str):
        self.id = cid
        self.source = source
        self.heading = heading
        self.text = text
        self.words = Counter(_words(text))
        self.trigrams = Counter(_trigrams(text))
        self.wvec: Dict[str, float] = {}
        self.tvec: Dict[str, float] = {}
        self.embedding: Optional[List[float]] = None

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()


class KnowledgeBase:
    def __init__(self, folder: Path, client=None, cache_path: Optional[Path] = None):
        self.folder = Path(folder)
        self.client = client
        self.cache_path = cache_path or (self.folder.parent / ".embed_cache.json")
        self.chunks: List[Chunk] = []
        self.files: List[str] = []
        self.semantic_enabled = False
        self.status = "not loaded"
        self._word_idf: Dict[str, float] = {}
        self._tri_idf: Dict[str, float] = {}
        self.load()

    def load(self) -> None:
        self.chunks = []
        self.files = []
        if not self.folder.exists():
            self.folder.mkdir(parents=True, exist_ok=True)

        cid = 0
        for path in sorted(self.folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw = path.read_text(encoding="utf-8", errors="replace")
            if not raw.strip():
                continue
            rel = str(path.relative_to(self.folder))
            self.files.append(rel)
            for heading, body in _split_into_sections(raw) or [("", raw)]:
                for piece in _pack_paragraphs(heading, body):
                    self.chunks.append(Chunk(cid, rel, heading, piece))
                    cid += 1

        self._build_idf()
        self._load_embeddings()
        self.status = (
            f"{len(self.files)} file(s), {len(self.chunks)} passage(s), "
            f"{'semantic + keyword' if self.semantic_enabled else 'keyword only'}"
        )

    def _build_idf(self) -> None:
        n = max(1, len(self.chunks))
        wdf: Counter = Counter()
        tdf: Counter = Counter()
        for c in self.chunks:
            wdf.update(c.words.keys())
            tdf.update(c.trigrams.keys())
        self._word_idf = {t: math.log(1 + n / (1 + df)) for t, df in wdf.items()}
        self._tri_idf = {t: math.log(1 + n / (1 + df)) for t, df in tdf.items()}
        for c in self.chunks:
            c.wvec = self._tfidf(c.words, self._word_idf)
            c.tvec = self._tfidf(c.trigrams, self._tri_idf)

    def _read_cache(self) -> Dict[str, List[float]]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_cache(self, cache: Dict[str, List[float]]) -> None:
        try:
            self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
            storage.save(self.cache_path)
        except Exception:
            pass

    def _embed(self, texts: List[str], task_type: str) -> Optional[List[List[float]]]:
        if self.client is None or not texts:
            return None
        try:
            from google.genai import types as gtypes
            vectors: List[List[float]] = []
            for i in range(0, len(texts), 32):
                batch = texts[i:i + 32]
                resp = self.client.models.embed_content(
                    model=EMBED_MODEL,
                    contents=batch,
                    config=gtypes.EmbedContentConfig(task_type=task_type),
                )
                vectors.extend([list(e.values) for e in resp.embeddings])
            return vectors
        except Exception as exc:
            print(f"[rag] embeddings unavailable ({exc}); using keyword search only")
            return None

    def _load_embeddings(self) -> None:
        if not self.chunks or self.client is None:
            self.semantic_enabled = False
            return
        cache = self._read_cache()
        missing = [c for c in self.chunks if c.digest not in cache]
        if missing:
            vectors = self._embed([c.text for c in missing], "RETRIEVAL_DOCUMENT")
            if vectors is None:
                self.semantic_enabled = False
                return
            for c, v in zip(missing, vectors):
                cache[c.digest] = v
            self._write_cache(cache)
        for c in self.chunks:
            c.embedding = cache.get(c.digest)
        self.semantic_enabled = all(c.embedding for c in self.chunks)

    def _tfidf(self, counts: Counter, idf: Dict[str, float]) -> Dict[str, float]:
        return {
            t: (1 + math.log(n)) * idf.get(t, 1.0)
            for t, n in counts.items()
            if idf.get(t, 1.0) > 0
        }

    def search(self, query: str, k: int = 4) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query or not self.chunks:
            return []

        qvec: Optional[List[float]] = None
        if self.semantic_enabled:
            got = self._embed([query], "RETRIEVAL_QUERY")
            qvec = got[0] if got else None

        qw = self._tfidf(Counter(_words(query)), self._word_idf)
        qt = self._tfidf(Counter(_trigrams(query)), self._tri_idf)

        scored: List[Tuple[float, float, float, Chunk]] = []
        for c in self.chunks:
            lex = 0.6 * _cosine_sparse(qw, c.wvec) + 0.4 * _cosine_sparse(qt, c.tvec)
            sem = _cosine_dense(qvec, c.embedding) if (qvec and c.embedding) else 0.0
            total = (0.65 * sem + 0.35 * lex) if qvec else lex
            scored.append((total, sem, lex, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for total, sem, lex, c in scored[:k]:
            if total < MIN_SCORE:
                continue
            results.append({
                "source": c.source,
                "section": c.heading or None,
                "text": c.text,
                "score": round(total, 4),
                "semantic": round(sem, 4),
                "keyword": round(lex, 4),
            })
        return results

    def topic_index(self, limit: int = 40) -> str:
        """A short list of what the knowledge base covers."""
        seen: List[str] = []
        for c in self.chunks:
            label = c.heading or c.source
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
        return "; ".join(seen) if seen else "(the knowledge base is empty)"

    def info(self) -> Dict[str, Any]:
        return {
            "files": self.files,
            "chunks": len(self.chunks),
            "semantic": self.semantic_enabled,
            "status": self.status,
        }
