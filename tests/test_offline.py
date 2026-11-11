"""API 키 없이 돌아가는 테스트: 청커·검색·에이전트 루프(가짜 클라이언트)."""

from types import SimpleNamespace as NS

from kgmp_agent.agent import run
from kgmp_agent.parse import legal_chunks
from kgmp_agent.tools import read_provision, search_regulations


def test_legal_chunker_paths():
    md = "1. 용어\n\n가. \"수율\"이란 이론 생산량에 대한 실생산량의 백분율을 말한다.\n\n8.1 제조공정관리\n\n- 사. 이론 생산량과 실생산량을 비교하여 수율관리기준을 벗어난 경우에는 원인을 조사한다.\n  - 1\\) 하위 항목"
    cs = legal_chunks(md, doc_id="t", doc_title="테스트", tier=3, kind="appendix", source="x")
    target = [c for c in cs if "수율관리기준을 벗어난" in c.text][0]
    assert target.path[-1].startswith("사목")
    assert "8.1 제조공정관리" in target.path
    assert "1) 하위 항목" in target.text  # 하위 항목은 같은 청크에


def test_search_finds_yield_clause():
    r = search_regulations("수율관리기준 벗어난 경우 원인 조사 대책")
    assert any("수율관리기준을 벗어난 경우에는 그 원인" in x["text"] for x in r["results"][:3])


class FakeClient:
    """search → read → submit 순서로 도구를 부르는 가짜 Claude."""

    def __init__(self):
        self.n = 0
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.n += 1
        usage = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if self.n == 1:
            blk = NS(type="tool_use", id="t1", name="search_regulations", input={"query": "수율관리기준 원인 조사"})
            return NS(stop_reason="tool_use", content=[blk], usage=usage)
        if self.n == 2:
            cid = search_regulations("수율관리기준 벗어난 경우 원인 조사 대책")["results"][0]["chunk_id"]
            self.cid = cid
            blk = NS(type="tool_use", id="t2", name="read_provision", input={"chunk_id": cid})
            return NS(stop_reason="tool_use", content=[blk], usage=usage)
        g = {"situation_summary": "s", "regulatory_classification": "c", "records_to_create": [], "handoff_to_quality": [],
             "prohibited": [], "open_questions": [], "limitations": "l",
             "actions": [{"step": 1, "owner": "공정엔지니어", "action": "원인 조사", "timing": "즉시",
                          "obligation": "법령상 의무", "citations": [self.cid]}]}
        blk = NS(type="tool_use", id="t3", name="submit_guidance", input=g)
        return NS(stop_reason="tool_use", content=[blk], usage=usage)


def test_agent_loop_with_fake_client():
    res = run("테스트 이벤트", verbose=False, client=FakeClient())
    assert res.stop == "submitted"
    assert res.guidance["actions"][0]["citations"]
    assert not res.invalid_citations
    assert read_provision(res.guidance["actions"][0]["citations"][0])["provisions"]
