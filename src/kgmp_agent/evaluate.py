"""검색 품질 실험: kordoc 파싱 결과를 Agentic RAG 에 쓸 수 있는가?

같은 질의 세트로 세 가지 색인을 비교한다.
  A. legal-hwp : kordoc(HWP) Markdown + 법령식 번호 청커   (이 데모의 기본값)
  B. legal-pdf : kordoc(PDF) Markdown + 법령식 번호 청커   (원본 형식에 따른 차이)
  C. native-hwp: kordoc --format chunks 그대로             (kordoc 기본 RAG 청크)
본문(법령·고시 조문)은 세 경우 모두 같은 조문 청크를 쓰고, 별표 처리 방식만 바꾼다.

정답 판정: 질의마다 '정답 조항에 들어 있는 문구'를 정해 두고, 그 문구를 포함한 청크가 상위 k 안에 있으면 적중.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .ingest import INDEX, build_chunks
from .search import BM25

# (공정 엔지니어가 실제로 물을 법한 질의, 정답 조항 문구, 정답 위치 설명)
GOLD = [
    ("타정 공정이 끝났는데 수율이 기준보다 낮게 나왔다. 무엇을 해야 하나", "수율관리기준을 벗어난 경우에는 그 원인을 조사하고 대책을 수립", "KGMP 8.1 사목"),
    ("수율관리기준의 정의", "제품별 연간 평균생산수율 범위", "KGMP 1. 자목"),
    ("제조기록서에 수율 이탈 사유를 적어야 하나", "수율과 수율관리기준을 벗어난 경우에는 그 사유", "KGMP 8.1 가목 5)"),
    ("예상 수율과 크게 다른 경우 일탈로 처리해야 하나", "예상 수율을 유의하게 벗어난", "고시 별표17 5.5"),
    ("기준을 벗어난 반제품을 다시 가공하려면 누구 승인이 필요한가", "품질(보증)부서 책임자의 승인", "KGMP 8.1 카목"),
    ("제조 중 일탈이 생기면 제조부서 책임자는 무엇을 하나", "일탈이 있는 경우에는 이를 조사하고 기록하여야 한다", "KGMP 3. 조직"),
    ("원인 분석 후 시정 예방조치 효과 확인", "시정 및 예방조치의 효과를 점검하고 평가", "고시 별표17 1.2 바목 13)"),
    ("제조공정이나 설비를 바꾸려면 어떤 절차를 거쳐야 하나", "기계설비, 원자재, 제조공정, 시험방법 등을", "KGMP 12. 변경관리"),
    ("제조업자가 GMP 를 지켜야 하는 법적 근거", "제조 및 품질관리 기준(이하 “제조 및 품질관리기준”이라 한다)", "약사법 제38조"),
    ("GMP 를 지키지 않으면 어떤 행정처분을 받나", "그 밖에 의약품 제조 및 품질관리기준을 준수하지 않은 경우", "규칙 별표8"),
    ("품질 문제로 출하한 제품을 회수해야 할 때 절차", "회수계획", "회수 규정·규칙"),
    ("연간 제품 품질 평가 때 일탈을 검토해야 하나", "제품품질평가", "고시 별표17 1.5"),
    ("수율 감시 알림 시스템도 밸리데이션 해야 하나", "컴퓨터화 시스템", "고시 별표9"),
    ("품질 위험관리 원칙", "품질위험관리", "고시 별표17 1.6"),
    ("공정 밸리데이션은 언제 다시 해야 하나", "재밸리데이션", "KGMP 6.2 / 별표13"),
    ("자율점검은 얼마나 자주", "자율점검", "KGMP 자율점검"),
]


# 에이전트가 시스템 프롬프트 지시대로 '법령 용어'로 바꿔 던질 법한 질의 (LLM 없이 Agentic 재질의 효과를 근사)
AGENT_QUERIES = [
    ["수율관리기준 벗어난 경우 원인 조사 대책", "이론 생산량 실생산량 수율"],
    ["수율관리기준 정의", "수율 정의 이론 생산량"],
    ["제조기록서 수율 수율관리기준 사유", "제조기록서 기재 사항"],
    ["예상 수율 벗어난 일탈 기록 조사", "수율 일탈"],
    ["기준일탈 반제품 재가공 승인", "재가공 품질(보증)부서 책임자 승인"],
    ["제조부서 책임자 일탈 조사 기록", "제조지시서 일탈 조사"],
    ["시정 및 예방조치 효과 점검 평가", "일탈 원인 분석 시정 예방조치"],
    ["변경관리 제조공정 기계설비 원자재 변경", "변경관리 승인 절차"],
    ["제조 및 품질관리기준 준수 의무 제조업자", "생산 관리의무"],
    ["행정처분 제조 및 품질관리기준 준수하지 않은 경우", "제조업무정지 품질관리기준"],
    ["회수 회수계획 품질 결함", "회수의무자 회수 절차"],
    ["제품품질평가 일탈 검토", "연간 품질평가"],
    ["컴퓨터화 시스템 밸리데이션", "컴퓨터화 시스템 데이터"],
    ["품질위험관리", "품질위험관리 원칙"],
    ["재밸리데이션 공정 밸리데이션 실시 시기", "공정 밸리데이션 변경"],
    ["자율점검 정기적", "자율점검 계획"],
]


def _metrics(chunks: list[dict]) -> dict:
    bm = BM25(chunks)
    ranks = []
    for q, gold, _ in GOLD:
        hits = bm.search(q, k=10)
        r = next((i + 1 for i, (_, c) in enumerate(hits) if gold in c["text"]), None)
        ranks.append(r)
    agent_ranks = []
    for (q, gold, _), aqs in zip(GOLD, AGENT_QUERIES):
        best = None  # 원 질의 + 재질의 결과 중 가장 좋은 순위 (에이전트는 여러 번 검색한 결과를 모두 본다)
        for qq in [q, *aqs]:
            hits = bm.search(qq, k=10)
            r = next((i + 1 for i, (_, c) in enumerate(hits) if gold in c["text"]), None)
            if r and (best is None or r < best):
                best = r
        agent_ranks.append(best)
    n = len(GOLD)
    appendix = [c for c in chunks if c["kind"] == "appendix"]
    return {
        "hit@1": sum(1 for r in ranks if r == 1) / n,
        "hit@3": sum(1 for r in ranks if r and r <= 3) / n,
        "hit@5": sum(1 for r in ranks if r and r <= 5) / n,
        "MRR@10": sum(1 / r for r in ranks if r) / n,
        "ranks": ranks,
        "agentic_hit@5": sum(1 for r in agent_ranks if r and r <= 5) / n,
        "agentic_MRR@10": sum(1 / r for r in agent_ranks if r) / n,
        "agent_ranks": agent_ranks,
        "appendix_chunks": len(appendix),
        "appendix_empty_path": sum(1 for c in appendix if not c["path"]) / max(len(appendix), 1),
        "appendix_median_len": statistics.median(len(c["text"]) for c in appendix) if appendix else 0,
        "gold_answerable": sum(1 for _, g, _ in GOLD if any(g in c["text"] for c in chunks)) / n,
    }


def main():
    variants = {
        "A. legal-hwp (기본)": dict(prefer="hwp", strategy="legal"),
        "B. legal-pdf": dict(prefer="pdf", strategy="legal"),
        "C. native-hwp (kordoc chunks)": dict(prefer="hwp", strategy="native"),
    }
    results = {}
    for name, kw in variants.items():
        print(f"\n▶ {name}")
        cs = [c.__dict__ for c in build_chunks(**kw, log=lambda *_: None)]
        results[name] = _metrics(cs)
        m = results[name]
        print(f"  hit@1 {m['hit@1']:.2f} | hit@3 {m['hit@3']:.2f} | hit@5 {m['hit@5']:.2f} | MRR@10 {m['MRR@10']:.3f}"
              f" || 재질의 포함 hit@5 {m['agentic_hit@5']:.2f} MRR {m['agentic_MRR@10']:.3f}"
              f" | 별표 청크 {m['appendix_chunks']} (경로 없음 {m['appendix_empty_path']:.0%}, 길이 중앙값 {m['appendix_median_len']:.0f})")
    print("\n질의별 정답 순위 (없음 = 상위 10 밖)")
    print(f"{'질의':<44}" + "".join(f"{k[:2]:>6}" for k in variants) + "  | 재질의 " + "".join(f"{k[:2]:>4}" for k in variants))
    for i, (q, _, where) in enumerate(GOLD):
        row = "".join(f"{(results[k]['ranks'][i] or '-'):>6}" for k in variants)
        row += "  |      " + "".join(f"{(results[k]['agent_ranks'][i] or '-'):>4}" for k in variants)
        print(f"{(q[:30] + ' [' + where + ']'):<44}{row}")
    out = INDEX / "retrieval_eval.json"
    out.write_text(json.dumps({"gold": GOLD, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
