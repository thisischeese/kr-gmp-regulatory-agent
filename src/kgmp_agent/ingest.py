"""수집한 원본 → 파싱 → 청크(JSONL) 생성."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from .parse import ROOT, Chunk, article_chunks, legal_chunks, native_kordoc_chunks, run_kordoc

RAW = ROOT / "data" / "raw"
PARSED = ROOT / "data" / "parsed"
INDEX = ROOT / "data" / "index"
USER_DOCS = RAW / "user_docs"
SUPPORTED = {".hwp", ".hwpx", ".pdf", ".docx", ".xlsx", ".xls", ".pptx"}


def load_manifest() -> dict:
    return yaml.safe_load((ROOT / "sources" / "manifest.yaml").read_text(encoding="utf-8"))


def _appendix_file(doc_id: str, prefer: str) -> Path | None:
    order = [prefer] + [f for f in ("hwp", "pdf") if f != prefer]
    for fmt in order:
        p = RAW / "appendix" / f"{doc_id}.{fmt}"
        if p.exists():
            return p
    return None


def build_chunks(prefer: str = "hwp", strategy: str = "legal", log=print) -> list[Chunk]:
    """strategy: legal(기본, kordoc Markdown + 법령식 청커) | native(kordoc --format chunks 그대로)"""
    PARSED.mkdir(parents=True, exist_ok=True)
    man = load_manifest()
    chunks: list[Chunk] = []
    for doc in man["documents"]:
        if doc["kind"] in ("law", "admrul"):
            p = RAW / "bodies" / f"{doc['id']}.json"
            if not p.exists():
                log(f"  ! 본문 없음(먼저 fetch): {doc['id']}")
                continue
            body = json.loads(p.read_text(encoding="utf-8"))
            cs = article_chunks(body, tier=doc["tier"])
        else:
            src = _appendix_file(doc["id"], prefer)
            if src is None:
                log(f"  ! 별표 파일 없음: {doc['id']}")
                continue
            if strategy == "native":
                js = run_kordoc(src, "chunks", ["--table-format", "gfm"])
                cs = native_kordoc_chunks(js, doc_id=doc["id"], doc_title=doc["title"], tier=doc["tier"],
                                          kind="appendix", source=src.name)
            else:
                md_path = PARSED / f"{src.name}.md"
                if not md_path.exists() or md_path.stat().st_mtime < src.stat().st_mtime:
                    md_path.write_text(run_kordoc(src), encoding="utf-8")
                cs = legal_chunks(md_path.read_text(encoding="utf-8"), doc_id=doc["id"], doc_title=doc["title"],
                                  tier=doc["tier"], kind="appendix", source=src.name)
        log(f"  {doc['id']:<24} {len(cs):>4} 청크")
        chunks += cs
    # 사용자가 넣은 가이드라인 등 (tier 5)
    if USER_DOCS.exists():
        for src in sorted(USER_DOCS.iterdir()):
            if src.suffix.lower() not in SUPPORTED:
                continue
            doc_id = "user_" + "".join(ch if ch.isalnum() else "_" for ch in src.stem)[:40]
            md = run_kordoc(src)
            (PARSED / f"{src.name}.md").write_text(md, encoding="utf-8")
            cs = legal_chunks(md, doc_id=doc_id, doc_title=src.stem, tier=5, kind="user_doc", source=src.name)
            log(f"  {doc_id:<24} {len(cs):>4} 청크 (사용자 문서)")
            chunks += cs
    return chunks


def save_chunks(chunks: list[Chunk], name: str = "chunks.jsonl") -> Path:
    INDEX.mkdir(parents=True, exist_ok=True)
    out = INDEX / name
    out.write_text("\n".join(c.to_json() for c in chunks) + "\n", encoding="utf-8")
    return out


def load_chunks(name: str = "chunks.jsonl") -> list[dict]:
    p = INDEX / name
    if not p.exists():
        raise FileNotFoundError(f"{p} 가 없습니다. 먼저 `uv run kgmp ingest` 를 실행하세요.")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
