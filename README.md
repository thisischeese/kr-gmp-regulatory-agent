# kr-gmp-regulatory-agent

국내 고형제 제조소에서 **공정 종료 후 수율 저하가 탐지되면**, 공정 엔지니어가 **다음에 해야 할 행동과 준수해야 할 요건**을
국내 법령·고시 근거와 함께 알려주는 **규정 준수 안내 에이전트(Agentic RAG) 데모**입니다.
멀티에이전트 시스템(탐지 에이전트 → **이 에이전트** → 품질담당자 에이전트) 중 가운데 역할을 합니다.

```
[수율 탐지 에이전트] --이벤트(JSON)--> [규정 준수 안내 에이전트] --조치 안내(JSON/MD)--> [공정 엔지니어 / 품질담당자 에이전트]
                                          │  search_regulations · read_provision · list_sources
                                          ▼
                     국가법령정보센터 법령·고시 (조문) + 별표 HWP/PDF (kordoc 파싱) → 법령식 청크 → BM25 색인
```

## 빠른 시작 — API 키만 붙여넣으면 됩니다

준비물: [uv](https://docs.astral.sh/uv/), Anthropic API 키. (색인이 저장소에 포함돼 있어 Node.js 없이도 바로 실행됩니다.)

```bash
uv sync                      # Python 3.12 가상환경 + 의존성
cp .env.example .env         # .env 를 열어 ANTHROPIC_API_KEY= 뒤에 키를 붙여넣기
uv run kgmp doctor           # 준비 상태 확인
uv run kgmp respond examples/yield_drop_event.json
```

결과는 화면과 `runs/<시각>_yield_drop_event.md · .json · .transcript.json` 에 저장됩니다.

```bash
uv run kgmp ask "타정 후 수율이 수율관리기준보다 낮다. 코팅 공정으로 넘겨도 되나?"
uv run kgmp search "수율관리기준 벗어난 경우 원인 조사"     # API 키 없이 검색만
uv run pytest -q                                            # API 키 없이 테스트
```

## 문서를 다시 받거나 추가하기 (Node.js 20+ 필요)

```bash
npm install                  # kordoc 4.24.65
uv run kgmp fetch            # 국가법령정보센터에서 법령·고시·별표 최신본 수집
uv run kgmp ingest           # kordoc 파싱 → 법령식 청크 → data/index/chunks.jsonl
uv run kgmp eval             # 청킹 방식별 검색 품질 비교 (API 키 불필요)
```

식약처 가이드라인·민원인 안내서, PIC/S 문서, 사내 SOP 등은 `data/raw/user_docs/` 에 HWP/PDF 로 넣고 `kgmp ingest` 를 실행하면 위계 5(가이드라인)로 함께 색인됩니다.

## 이 저장소가 답하는 세 가지

| # | 질문 | 문서 |
|---|---|---|
| 1 | 국내 제조업체가 규정 준수를 위해 봐야 하는 법·가이던스 문서의 종류 | [docs/01_regulatory_landscape.md](docs/01_regulatory_landscape.md) |
| 2 | kordoc 으로 파싱한 문서를 Agentic RAG 에 쓸 수 있는가 (실험) | [docs/02_kordoc_parsing_analysis.md](docs/02_kordoc_parsing_analysis.md) |
| 3 | API 키만 넣으면 실험할 수 있는 데모 | 위 빠른 시작 |

**요약**
- 색인: 약사법·시행령·시설기준령·「의약품 등의 안전에 관한 규칙」(+[별표 1] KGMP, [별표 8] 행정처분), 식약처 고시 「의약품 제조 및 품질관리에 관한 규정」(+[별표 17]·[별표 13]·[별표 9]), 「의약품등 회수에 관한 규정」 — 11종, 1,252개 청크
- 핵심 근거: KGMP 8.1 사목 "이론 생산량과 실생산량을 비교하여 수율관리기준을 벗어난 경우에는 그 원인을 조사하고 대책을 수립하여 시행", 고시 [별표 17] 5.5 마목 "예상 수율을 유의하게 벗어난 모든 일탈은 기록하고 조사"
- kordoc: HWP·PDF 결과가 공백을 빼면 100% 일치, 표 340행 구조 보존. 다만 기본 RAG 청크는 법령 번호 위계를 못 잡아(경로 없음 50%) 법령식 청커로 보완 → 검색 MRR 0.389 → 0.485
- 단일 키워드 검색만으로는 정답 조항 hit@5 0.62 → 질의를 법령 용어로 바꿔 여러 번 찾는 **에이전트 방식**이 필요

## 에이전트 동작

- 모델: `claude-opus-5-5` (기본, `KGMP_MODEL` 로 변경), effort `high` (`KGMP_EFFORT`)
- 도구: `search_regulations`(BM25), `read_provision`(조항 전문 + 앞뒤 조항), `list_sources`, `submit_guidance`(결과 제출, strict 스키마)
- 안전장치
  - `read_provision` 으로 읽지 않은 조항을 인용하면 제출을 한 번 반려하고 확인하게 함
  - 의무 수준을 `법령상 의무 / 고시 요건 / 가이드라인 권고 / 근거 미확인(일반 관행)` 으로 구분
  - 출하 판정·재가공 승인·일탈 종결 같은 결정은 품질(보증)부서로 넘김, 이벤트에 없는 사실은 `open_questions` 로
  - 서버 측 대체 모델(`fallbacks: "default"`)을 켜 두어, 안전 분류기가 요청을 거절하면 다른 모델로 자동 재시도합니다. 원치 않으면 `agent.py` 에서 `betas`·`fallbacks` 두 줄을 지우세요.
- 출력(JSON, 품질담당자 에이전트 입력용): `situation_summary`, `regulatory_classification`, `actions[]`(step·owner·action·timing·obligation·citations), `records_to_create`, `handoff_to_quality`, `prohibited`, `open_questions`, `limitations`

## 한계

- 식약처 가이드라인·민원인 안내서는 실험 환경에서 식약처 사이트 접속이 차단돼 색인하지 못했습니다(국내에서 받아 `user_docs/` 에 넣으면 됩니다).
- 이 저장소의 안내는 규정 조항 검색 결과를 정리한 것이며, 회사 SOP·품질(보증)부서 판단을 대신하지 않습니다.
- 법령은 수시로 개정되므로 사용 전 `kgmp fetch` 로 최신본을 받으세요. 법령 원문 출처: 국가법령정보센터(law.go.kr).

## 구성

```
src/kgmp_agent/
  fetch.py     국가법령정보센터 수집 (조문 HTML → JSON, 별표 HWP/PDF)
  parse.py     kordoc 호출, 법령식 번호 청커, 조문 청커
  ingest.py    수집본 → 청크(JSONL)
  search.py    한국어 BM25 (어절 + 2-gram)
  tools.py     에이전트 도구 정의·실행
  agent.py     Claude 도구 사용 루프, 시스템 프롬프트, 결과 렌더링
  evaluate.py  청킹 방식별 검색 실험
  cli.py       kgmp 명령
sources/manifest.yaml   수집 대상과 권장 추가 문서
examples/yield_drop_event.json   Lek 공개 데이터의 최저 수율 배치를 바탕으로 만든 가상 탐지 이벤트
data/raw · data/parsed · data/index   원본, kordoc 결과, 청크·실험 결과
```
