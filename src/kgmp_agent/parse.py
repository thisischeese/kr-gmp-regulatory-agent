"""문서 파싱과 청킹.

- 별표·가이드라인 파일(HWP·HWPX·PDF·DOCX 등) → kordoc(Node.js) 으로 Markdown 변환
- Markdown → 한국 법령식 번호 체계(1. > 1.1 > 가. > 1) > 가) …)를 따라 '목' 단위 청크
- 법령·고시 본문(JSON) → 조문 단위 청크 (긴 조문은 항 단위로 나눔)

청크마다 문서 위계(tier)와 '어디에 있는 조항인지'(path, citation)를 붙여
에이전트가 근거를 정확히 인용할 수 있게 한다.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ─────────────────────────────── kordoc 호출 ───────────────────────────────


def kordoc_bin() -> list[str]:
    local = ROOT / "node_modules" / ".bin" / "kordoc"
    if local.exists():
        return [str(local)]
    if shutil.which("kordoc"):
        return ["kordoc"]
    return ["npx", "--yes", "kordoc@4.24.65"]


def run_kordoc(src: Path, fmt: str = "markdown", extra: list[str] | None = None) -> str:
    """kordoc CLI 로 파일을 변환해 결과 문자열을 돌려준다. fmt: markdown | chunks | json"""
    env = {**os.environ, "KORDOC_OFFLINE": "1"}  # 외부 통신 차단 (폐쇄망과 같은 조건)
    suffix = ".md" if fmt == "markdown" else ".json"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / f"out{suffix}"
        cmd = [*kordoc_bin(), str(src), "-o", str(out)]
        if fmt != "markdown":
            cmd += ["--format", fmt]
        cmd += extra or []
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
        if p.returncode != 0 or not out.exists():
            raise RuntimeError(f"kordoc 실패: {src}\n{p.stderr[-2000:]}")
        return out.read_text(encoding="utf-8")


# ─────────────────────────────── 청크 구조 ───────────────────────────────


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    tier: int
    kind: str                     # law | admrul | appendix | user_doc
    path: list[str]               # 문서 안 위치 (장·조 또는 1. > 1.1 > 가.)
    citation: str                 # 사람이 읽는 인용 표기
    text: str
    source: str                   # 원본 파일 / URL
    parser: str                   # law_html | kordoc+legal | kordoc_native
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ─────────────────────── 법령식 번호 체계 청커 (Markdown) ───────────────────────

GANADA = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허고노도로모보소오조초코토포호"

LEVELS: list[tuple[int, re.Pattern]] = [
    (0, re.compile(r"^#{1,6}\s+(.+)$")),                       # Markdown 제목
    (0, re.compile(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\.\s*.+)$")),               # Ⅰ. 일반기준
    (3, re.compile(r"^(?:\*\*)?(\d+\.\d+\.\d+)\.?\s+(.+)$")),   # 1.2.3
    (2, re.compile(r"^(?:\*\*)?(\d+\.\d+)\.?\s+(.+)$")),         # 1.2 (호의 세부)
    (1, re.compile(r"^(?:\*\*)?(\d+(?:의\d+)?)\.\s+(.+)$")),     # 1. / 2의2.
    (4, re.compile(rf"^([{GANADA}](?:의\d+)?)\.\s+(.+)$")),      # 가. (목)
]
SUB = re.compile(rf"^(\d+\)|[{GANADA}]\)|\(\d+\)|\([{GANADA}]\))\s*")  # 1) 가) (1) (가) — 목 안에 둠

MAX_CHARS = 1800
MIN_CHARS = 40


def _label(level: int, m: re.Match) -> str:
    if level == 0:
        return m.group(1).strip("* ").strip()
    num, rest = m.group(1), m.group(2).strip("* ").strip()
    if level == 4:  # 목은 번호 + 앞부분만 (긴 문장 전체를 경로에 넣지 않음)
        return f"{num}목({rest[:24]}{'…' if len(rest) > 24 else ''})"
    return f"{num} {rest[:40]}"


def _table_to_lines(tbl_html: str) -> list[str]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl_html, re.S)
    out = []
    for r in rows:
        cells = [htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if any(cells):
            out.append(" | ".join(cells))
    return out


def legal_chunks(md: str, *, doc_id: str, doc_title: str, tier: int, kind: str, source: str,
                 parser: str = "kordoc+legal") -> list[Chunk]:
    """kordoc Markdown → '목' 단위 청크. 표는 머리행을 붙여 행 묶음으로 나눈다."""
    lines = md.replace("\r", "").split("\n")
    stack: dict[int, str] = {}
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_path: list[str] = []

    def cur_path() -> list[str]:
        return [stack[k] for k in sorted(stack)]

    def flush():
        nonlocal buf
        text = "\n".join(l for l in buf if l.strip()).strip()
        buf = []
        if len(text) < MIN_CHARS:
            return
        for part in _split_long(text):
            n = len(chunks) + 1
            chunks.append(Chunk(
                id=f"{doc_id}#{n:04d}", doc_id=doc_id, doc_title=doc_title, tier=tier, kind=kind,
                path=list(buf_path), citation=f"{doc_title} " + " > ".join(buf_path) if buf_path else doc_title,
                text=part, source=source, parser=parser))

    i = 0
    while i < len(lines):
        raw = lines[i]
        # PDF 결과는 항목을 Markdown 목록("- 가.", "  - 1\\)")으로 내므로 목록 기호와 이스케이프를 걷어낸다
        line = re.sub(r"^\s*[-*+]\s+", "", raw).strip().replace("\\)", ")").replace("\\.", ".")
        if line.startswith("<table"):
            # 표 블록: </table> 까지 모은다
            j = i
            block = []
            while j < len(lines):
                block.append(lines[j])
                if "</table>" in lines[j]:
                    break
                j += 1
            flush()
            rows = _table_to_lines("\n".join(block))
            if rows:
                head, body = rows[0], rows[1:] or []
                step = 8
                for k in range(0, max(len(body), 1), step):
                    buf_path[:] = cur_path() + [f"표(행 {k + 1}–{min(k + step, len(body))})"]
                    buf = [f"[표 머리행] {head}", *body[k:k + step]]
                    flush()
                buf_path[:] = cur_path()
            i = j + 1
            continue
        matched = False
        if not SUB.match(line):
            for level, pat in LEVELS:
                m = pat.match(line)
                if m:
                    flush()
                    for k in [k for k in stack if k >= level]:
                        del stack[k]
                    lab = _label(level, m)
                    if level == 0 and lab.replace(" ", "") in doc_title.replace(" ", ""):
                        buf.append(line)  # 문서 제목과 같은 머리말은 경로에 넣지 않음
                        matched = True
                        break
                    stack[level] = lab
                    buf_path[:] = cur_path()
                    buf.append(line)
                    matched = True
                    break
        if not matched:
            buf.append(line)
        i += 1
    flush()
    return chunks


def _split_long(text: str) -> list[str]:
    """긴 청크는 1) 단위로 나누되 첫 문장(목의 본문)을 각 조각 앞에 붙인다."""
    if len(text) <= MAX_CHARS:
        return [text]
    ls = text.split("\n")
    lead, rest = ls[0], ls[1:]
    parts, cur = [], [lead]
    for l in rest:
        if len("\n".join(cur)) + len(l) > MAX_CHARS and SUB.match(l):
            parts.append("\n".join(cur))
            cur = [lead + " (계속)", l]
        else:
            cur.append(l)
    parts.append("\n".join(cur))
    return parts


# ─────────────────────── 법령·고시 본문 (조문 JSON) ───────────────────────


HANG = re.compile(r"^[①-⑳]")
HO = re.compile(r"^\d+(?:의\d+)?\.\s")


def _split_article(head: str, text: str) -> list[str]:
    """긴 조문을 항(①…)·호(1. …) 경계에서 나눈다. 조각마다 조 제목과 소속 항의 첫 줄을 붙인다."""
    if len(text) <= MAX_CHARS:
        return [text]
    lines = text.split("\n")
    parts: list[str] = []
    cur: list[str] = []
    hang_lead = ""
    for ln in lines:
        boundary = bool(HANG.match(ln) or HO.match(ln))
        if boundary and cur and len("\n".join(cur)) + len(ln) > MAX_CHARS:
            parts.append("\n".join(cur))
            cur = [f"{head} (계속)"]
            if not HANG.match(ln) and hang_lead:
                cur.append(hang_lead[:200] + (" …" if len(hang_lead) > 200 else ""))
        if HANG.match(ln) or ln.startswith(head.split("(")[0]):
            hang_lead = ln
        cur.append(ln)
    parts.append("\n".join(cur))
    return parts


def article_chunks(body: dict, *, tier: int) -> list[Chunk]:
    out: list[Chunk] = []
    for n, a in enumerate(body["articles"], 1):
        head = f"{a['no']}({a['title']})"
        path = [p for p in [a.get("chapter", ""), head] if p]
        text = a["text"]
        parts = _split_article(head, text)
        for k, p in enumerate(parts):
            out.append(Chunk(
                id=f"{body['id']}#{a['no']}" + (f"-{k + 1}" if len(parts) > 1 else ""),
                doc_id=body["id"], doc_title=body["title"], tier=tier, kind=body["kind"], path=path,
                citation=f"{body['title']} {a['no']}({a['title']})", text=p,
                source=body["source_url"], parser="law_html", meta={"effective": body.get("meta", "")}))
    return out


def native_kordoc_chunks(js: str, *, doc_id: str, doc_title: str, tier: int, kind: str, source: str) -> list[Chunk]:
    """비교용: kordoc 의 --format chunks 결과를 그대로 청크로 쓴다."""
    data = json.loads(js)
    items = data if isinstance(data, list) else data.get("chunks", [])
    out = []
    for n, c in enumerate(items, 1):
        bc = [b[:40] for b in c.get("breadcrumb", [])]
        out.append(Chunk(id=f"{doc_id}~{n:04d}", doc_id=doc_id, doc_title=doc_title, tier=tier, kind=kind,
                         path=bc, citation=f"{doc_title} " + " > ".join(bc), text=c.get("text", ""),
                         source=source, parser="kordoc_native"))
    return out
