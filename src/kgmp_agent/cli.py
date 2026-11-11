"""kgmp 명령: doctor | fetch | ingest | search | ask | respond | eval"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from .parse import ROOT

load_dotenv(ROOT / ".env")

RUNS = ROOT / "runs"


def cmd_doctor(_):
    ok = True
    print(f"Python      : {sys.version.split()[0]}")
    node = shutil.which("node")
    print(f"Node.js     : {'있음' if node else '없음 (kordoc 파싱에 필요: Node 20+)'}")
    print(f"kordoc      : {'설치됨' if (ROOT / 'node_modules' / '.bin' / 'kordoc').exists() else '미설치 → npm install'}")
    idx = ROOT / "data" / "index" / "chunks.jsonl"
    print(f"색인        : {'있음 (' + str(sum(1 for _ in idx.open())) + '개 청크)' if idx.exists() else '없음 → uv run kgmp ingest'}")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    print(f"API 키      : {'설정됨 (' + key[:10] + '…)' if key else '없음 → .env 에 ANTHROPIC_API_KEY 입력'}")
    print(f"모델        : {os.getenv('KGMP_MODEL', 'claude-opus-5-5')} (effort={os.getenv('KGMP_EFFORT', 'high')})")
    ok = bool(key) and idx.exists()
    print("\n준비 완료: `uv run kgmp respond examples/yield_drop_event.json`" if ok else "\n위 항목을 채운 뒤 다시 실행하세요.")


def cmd_fetch(_):
    from .fetch import fetch_all
    from .ingest import RAW, load_manifest
    for r in fetch_all(load_manifest(), RAW):
        print("  ", r)


def cmd_ingest(a):
    from .ingest import build_chunks, save_chunks
    cs = build_chunks(prefer=a.prefer, strategy=a.strategy)
    out = save_chunks(cs, "chunks.jsonl" if a.strategy == "legal" else f"chunks_{a.strategy}.jsonl")
    print(f"→ {out} ({len(cs)}개 청크)")


def cmd_search(a):
    from .tools import search_regulations
    for r in search_regulations(a.query, top_k=a.k, max_tier=a.max_tier)["results"]:
        print(f"[{r['score']:6.2f}] {r['chunk_id']}  (위계 {r['tier']})\n   {r['citation']}\n   {r['text'][:220].replace(chr(10), ' / ')}\n")


def _run_agent(prompt: str, label: str):
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 가 없습니다. .env.example 을 .env 로 복사하고 키를 붙여넣으세요.")
    from .agent import render_markdown, run
    from .tools import read_provision
    res = run(prompt)
    RUNS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = RUNS / f"{stamp}_{label}"
    payload = {"guidance": res.guidance, "stop": res.stop, "usage": res.usage, "tool_calls": res.tool_calls,
               "invalid_citations": res.invalid_citations}
    base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    base.with_suffix(".transcript.json").write_text(json.dumps(res.transcript, ensure_ascii=False, indent=1, default=str),
                                                    encoding="utf-8")
    if not res.guidance:
        print(f"\n제출된 결과가 없습니다 (stop={res.stop}). 대화 기록: {base}.transcript.json")
        return

    def cite(cid: str) -> str:
        p = read_provision(cid, 0).get("provisions", [])
        return f"`{cid}` {p[0]['citation']}" if p else f"`{cid}`(없음)"

    md = render_markdown(res.guidance, cite)
    base.with_suffix(".md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\n— 도구 호출 {len(res.tool_calls)}회, 토큰 입력 {res.usage['input_tokens']:,} / 캐시 읽기 "
          f"{res.usage['cache_read_input_tokens']:,} / 출력 {res.usage['output_tokens']:,}")
    if res.invalid_citations:
        print(f"⚠ 확인되지 않은 인용: {res.invalid_citations}")
    print(f"저장: {base}.md / .json / .transcript.json")


def cmd_ask(a):
    _run_agent(a.question, "ask")


def cmd_respond(a):
    event = json.loads(Path(a.event).read_text(encoding="utf-8"))
    prompt = ("수율 저하 탐지 에이전트가 아래 이벤트를 보냈다. 공정 엔지니어가 다음에 해야 할 행동과 준수해야 할 요건을 "
              "국내 법령·고시 근거로 정리해 submit_guidance 로 제출하라.\n\n"
              f"```json\n{json.dumps(event, ensure_ascii=False, indent=2)}\n```")
    _run_agent(prompt, Path(a.event).stem)


def cmd_eval(_):
    from .evaluate import main
    main()


def main(argv=None):
    p = argparse.ArgumentParser(prog="kgmp", description="국내 GMP 규정 준수 안내 에이전트 데모")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("doctor", help="실행 준비 상태 확인").set_defaults(f=cmd_doctor)
    sp.add_parser("fetch", help="국가법령정보센터에서 문서 수집").set_defaults(f=cmd_fetch)
    s = sp.add_parser("ingest", help="kordoc 파싱 + 청크 색인 생성")
    s.add_argument("--prefer", default="hwp", choices=["hwp", "pdf"], help="별표 원본 형식")
    s.add_argument("--strategy", default="legal", choices=["legal", "native"], help="청킹 방식")
    s.set_defaults(f=cmd_ingest)
    s = sp.add_parser("search", help="색인 검색 (API 키 불필요)")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--max-tier", type=int)
    s.set_defaults(f=cmd_search)
    s = sp.add_parser("ask", help="자유 질문")
    s.add_argument("question")
    s.set_defaults(f=cmd_ask)
    s = sp.add_parser("respond", help="탐지 이벤트(JSON)에 대한 조치 안내")
    s.add_argument("event")
    s.set_defaults(f=cmd_respond)
    sp.add_parser("eval", help="검색 품질 실험 (API 키 불필요)").set_defaults(f=cmd_eval)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
