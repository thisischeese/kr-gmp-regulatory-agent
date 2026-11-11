"""규정 준수 안내 에이전트 (Agentic RAG).

수율 저하 탐지 에이전트가 보낸 이벤트(JSON)를 받아, 국내 법령·고시·별표를 스스로 검색·열람한 뒤
공정 엔지니어가 다음에 해야 할 일과 지켜야 할 것을 근거 조항과 함께 돌려준다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import anthropic

from .tools import TOOLS, run_tool

MODEL = os.getenv("KGMP_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("KGMP_EFFORT", "high")      # low | medium | high | xhigh | max
MAX_TURNS = int(os.getenv("KGMP_MAX_TURNS", "24"))

SYSTEM = """당신은 대한민국 의약품(고형제) 제조소의 GMP 규정 준수 안내 에이전트다.
멀티에이전트 시스템 안에서 '수율 저하 탐지 에이전트'가 보낸 이벤트를 받아, 공정 엔지니어가 다음에 해야 할 행동과
지켜야 할 요건을 국내 법령·고시 근거와 함께 정리한다. 결과는 품질담당자 에이전트에게도 전달된다.

## 근거 자료
- 도구로 검색할 수 있는 색인: 약사법, 약사법 시행령, 의약품 등의 안전에 관한 규칙(총리령)과 그 [별표 1] 의약품 제조 및
  품질관리기준(KGMP)·[별표 8] 행정처분의 기준, 시설기준령, 식약처 고시 「의약품 제조 및 품질관리에 관한 규정」과
  [별표 17] 완제의약품 제조·[별표 13] 적격성평가와 밸리데이션·[별표 9] 컴퓨터화 시스템, 「의약품등 회수에 관한 규정」.
- 위계: 1 법률 > 2 대통령령 > 3 총리령 > 4 고시 > 5 가이드라인(구속력 없음). 상위 근거를 우선 인용한다.

## 작업 방식
1. 이벤트에서 공정 단계·이탈 정도·출하 여부 등 판단 요소를 뽑는다.
2. search_regulations 로 여러 번 검색한다. 법령 용어로 질의한다(수율관리기준, 일탈, 기준일탈, 원인 조사, 대책, 제조기록서,
   재가공, 품질(보증)부서 승인, 시정 및 예방조치, 변경관리, 재밸리데이션, 제품품질평가, 회수 등).
3. 인용할 조항은 반드시 read_provision 으로 전문을 확인한다. 조항이 다른 조항(예: '제4.1호거목', '별표 1')을 가리키면 따라가서 읽는다.
4. 마지막에 submit_guidance 를 한 번 호출해 결과를 제출한다.

## 원칙
- 근거 없이 의무를 만들지 않는다. 색인에서 근거를 찾지 못한 행동은 obligation 을 '근거 미확인(일반 관행)'으로 표시한다.
- '법령상 의무'(법률·대통령령·총리령과 그 별표), '고시 요건'(식약처 고시와 그 별표), '가이드라인 권고'를 구분한다.
- 공정 엔지니어 단독으로 할 수 없는 결정(출하 판정, 재가공 승인, 일탈 종결, CAPA 승인 등)은 품질(보증)부서 소관으로 넘긴다.
- 이벤트에 없는 사실(원인, 출하 여부 등)을 지어내지 말고 open_questions 에 적는다.
- 회사 SOP 가 정한 기한·양식은 알 수 없으므로 '사내 SOP 에 따름'으로 표기한다.
- 한국어로 간결하게 쓴다.
"""


@dataclass
class RunResult:
    guidance: dict | None
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    invalid_citations: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop: str = ""


def _client():
    return anthropic.Anthropic()  # ANTHROPIC_API_KEY 등 환경변수에서 자격 증명을 읽는다


def run(event_or_question: str, *, verbose: bool = True, client=None) -> RunResult:
    client = client or _client()
    messages: list[dict] = [{"role": "user", "content": event_or_question}]
    res = RunResult(guidance=None)
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    for turn in range(MAX_TURNS):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},          # 시스템·도구·대화 앞부분 캐시
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                           # 안전 분류기가 거절하면 서버가 대체 모델로 재시도
        )
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0
        if resp.stop_reason == "refusal":
            res.stop = "refusal"
            break
        messages.append({"role": "assistant", "content": resp.content})  # 블록 그대로 되돌려 보낸다

        if resp.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            res.stop = resp.stop_reason or "end_turn"
            if res.guidance is None:  # 제출 없이 끝나면 한 번 더 요청
                messages.append({"role": "user", "content": "조사를 마쳤다면 submit_guidance 로 결과를 제출하세요."})
                continue
            break

        results = []
        finished = False
        for tu in tool_uses:
            args = tu.input if isinstance(tu.input, dict) else {}
            res.tool_calls.append({"name": tu.name, "input": args})
            if verbose:
                shown = json.dumps(args, ensure_ascii=False)
                print(f"  → {tu.name} {shown[:140] if tu.name != 'submit_guidance' else ''}")
            if tu.name == "submit_guidance":
                bad = [c for a in args.get("actions", []) for c in a.get("citations", []) if c not in res.read_ids]
                if bad and not finished and turn < MAX_TURNS - 2 and not res.invalid_citations:
                    res.invalid_citations = bad
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": f"read_provision 으로 확인하지 않은 chunk_id 가 인용됨: {bad}. "
                                               "해당 조항을 읽어 확인하거나 인용에서 빼고 다시 제출하세요."})
                    continue
                res.guidance = args
                res.invalid_citations = bad
                finished = True
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "제출 완료"})
            else:
                out = run_tool(tu.name, args)
                if tu.name == "read_provision":
                    for p in json.loads(out).get("provisions", []):
                        res.read_ids.add(p["chunk_id"])
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})
        if finished:
            res.stop = "submitted"
            break

    res.usage = usage
    res.transcript = [m if isinstance(m["content"], str) else
                      {"role": m["role"], "content": [b if isinstance(b, dict) else
                                       (b.model_dump() if hasattr(b, "model_dump") else vars(b)) for b in m["content"]]}
                      for m in messages]
    return res


def render_markdown(g: dict, citations_lookup) -> str:
    """제출 결과를 사람이 읽는 보고서로."""
    out = ["# 공정 엔지니어 조치 안내", "", f"**상황**: {g['situation_summary']}", "",
           f"**규정상 분류**: {g['regulatory_classification']}", "", "## 해야 할 일", "",
           "| # | 담당 | 조치 | 시점 | 근거 수준 | 근거 |", "|---|---|---|---|---|---|"]
    for a in sorted(g["actions"], key=lambda x: x["step"]):
        cites = "<br>".join(citations_lookup(c) for c in a["citations"]) or "-"
        out.append(f"| {a['step']} | {a['owner']} | {a['action']} | {a['timing']} | {a['obligation']} | {cites} |")
    for title, key in [("작성·보관할 기록", "records_to_create"), ("품질담당자에게 넘길 사항", "handoff_to_quality"),
                       ("하면 안 되는 것", "prohibited"), ("확인이 필요한 정보", "open_questions")]:
        out += ["", f"## {title}", ""] + [f"- {x}" for x in g[key]]
    out += ["", "## 근거의 한계", "", g["limitations"]]
    return "\n".join(out)
