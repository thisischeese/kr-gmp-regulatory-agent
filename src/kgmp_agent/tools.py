"""에이전트가 쓰는 도구 (검색·조문 읽기·문서 목록·결과 제출)."""

from __future__ import annotations

import json
from functools import lru_cache

from .ingest import load_chunks, load_manifest
from .search import BM25

OWNERS = ["공정엔지니어", "제조부서 책임자", "품질(보증)부서", "품질관리(시험)부서", "공정엔지니어+품질(보증)부서"]
OBLIGATIONS = ["법령상 의무", "고시 요건", "가이드라인 권고", "근거 미확인(일반 관행)"]


@lru_cache(maxsize=1)
def _store():
    chunks = load_chunks()
    by_id = {c["id"]: i for i, c in enumerate(chunks)}
    return chunks, by_id, BM25(chunks)


def _snippet(text: str, n: int = 700) -> str:
    return text if len(text) <= n else text[:n] + " …"


def search_regulations(query: str, top_k: int = 8, max_tier: int | None = None,
                       doc_ids: list[str] | None = None) -> dict:
    chunks, _, bm = _store()
    hits = bm.search(query, k=max(1, min(top_k, 15)), doc_ids=doc_ids or None, max_tier=max_tier)
    return {"query": query, "results": [
        {"chunk_id": c["id"], "score": round(s, 2), "tier": c["tier"], "citation": c["citation"],
         "text": _snippet(c["text"])} for s, c in hits]}


def read_provision(chunk_id: str, neighbors: int = 1) -> dict:
    chunks, by_id, _ = _store()
    if chunk_id not in by_id:
        return {"error": f"없는 chunk_id: {chunk_id}. search_regulations 결과의 chunk_id 를 그대로 쓰세요."}
    i = by_id[chunk_id]
    lo, hi = max(0, i - neighbors), min(len(chunks), i + neighbors + 1)
    out = []
    for c in chunks[lo:hi]:
        if c["doc_id"] != chunks[i]["doc_id"]:
            continue
        out.append({"chunk_id": c["id"], "is_target": c["id"] == chunk_id, "citation": c["citation"],
                    "tier": c["tier"], "text": c["text"], "source": c["source"],
                    "effective": c.get("meta", {}).get("effective", "")})
    return {"provisions": out}


def list_sources() -> dict:
    chunks, _, _ = _store()
    man = load_manifest()
    counts: dict[str, int] = {}
    for c in chunks:
        counts[c["doc_id"]] = counts.get(c["doc_id"], 0) + 1
    docs = [{"doc_id": d["id"], "title": d["title"], "tier": d["tier"], "chunks": counts.get(d["id"], 0),
             "why": d["why"]} for d in man["documents"]]
    for doc_id, n in counts.items():
        if doc_id.startswith("user_"):
            docs.append({"doc_id": doc_id, "title": doc_id[5:], "tier": 5, "chunks": n, "why": "사용자 추가 문서"})
    return {"tier_legend": "1 법률 > 2 대통령령 > 3 총리령 > 4 고시(행정규칙) > 5 가이드라인·안내서",
            "documents": docs,
            "not_indexed_recommended": [u["title"] for u in man.get("user_docs_recommended", [])
                                        if not any(u["title"][:8] in d["title"] for d in docs)]}


# ── Claude 도구 정의 ──

TOOLS = [
    {
        "name": "search_regulations",
        "description": (
            "국내 의약품 GMP 관련 법령·고시·별표 색인을 BM25 로 검색한다. 한국어 키워드 검색이므로 "
            "법령에서 쓰는 용어로 질의할 것(예: '수율관리기준', '일탈 조사', '시정 및 예방조치', '변경관리', "
            "'재가공 승인', '제조기록서'). 동의어로 여러 번 검색해도 된다. 결과의 chunk_id 로 read_provision 을 호출해 전문을 읽는다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (한국어)"},
                "top_k": {"type": "integer", "description": "결과 수 (기본 8, 최대 15)"},
                "max_tier": {"type": "integer", "description": "이 위계까지만 (1 법률 … 4 고시, 5 가이드라인)"},
                "doc_ids": {"type": "array", "items": {"type": "string"},
                            "description": "특정 문서로 한정 (list_sources 의 doc_id)"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_provision",
        "description": "chunk_id 의 조항 전문과 앞뒤 조항(neighbors 개)을 읽는다. 인용 전에 반드시 전문을 확인할 것.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "neighbors": {"type": "integer", "description": "앞뒤로 함께 볼 조항 수 (기본 1)"},
            },
            "required": ["chunk_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sources",
        "description": "색인된 문서 목록과 위계, 아직 색인되지 않은 권장 가이드라인 목록을 본다.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "submit_guidance",
        "description": (
            "조사를 마친 뒤 공정 엔지니어용 조치 안내를 제출한다. 마지막에 반드시 한 번 호출한다. "
            "citations 에는 이번 대화에서 read_provision 으로 확인한 chunk_id 만 넣는다."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "situation_summary": {"type": "string", "description": "상황 요약 (2~4문장)"},
                "regulatory_classification": {"type": "string",
                                              "description": "규정상 무엇에 해당하는지 (예: 수율관리기준 이탈 → 일탈 조사 대상)"},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "integer"},
                            "owner": {"type": "string", "enum": OWNERS},
                            "action": {"type": "string"},
                            "timing": {"type": "string", "description": "즉시 / 조사 중 / 출하 판정 전 / 후속 등"},
                            "obligation": {"type": "string", "enum": OBLIGATIONS},
                            "citations": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["step", "owner", "action", "timing", "obligation", "citations"],
                        "additionalProperties": False,
                    },
                },
                "records_to_create": {"type": "array", "items": {"type": "string"},
                                      "description": "작성·보관해야 할 기록"},
                "handoff_to_quality": {"type": "array", "items": {"type": "string"},
                                       "description": "품질담당자 에이전트/담당자에게 넘길 사항"},
                "prohibited": {"type": "array", "items": {"type": "string"},
                               "description": "해서는 안 되는 행동 (예: 품질부서 승인 없는 재가공)"},
                "open_questions": {"type": "array", "items": {"type": "string"},
                                   "description": "판단에 필요한데 이벤트에 없는 정보"},
                "limitations": {"type": "string", "description": "근거의 한계 (색인되지 않은 가이드라인 등)"},
            },
            "required": ["situation_summary", "regulatory_classification", "actions", "records_to_create",
                         "handoff_to_quality", "prohibited", "open_questions", "limitations"],
            "additionalProperties": False,
        },
    },
]


def run_tool(name: str, args: dict) -> str:
    if name == "search_regulations":
        out = search_regulations(**args)
    elif name == "read_provision":
        out = read_provision(**args)
    elif name == "list_sources":
        out = list_sources()
    else:
        out = {"error": f"알 수 없는 도구: {name}"}
    return json.dumps(out, ensure_ascii=False)
