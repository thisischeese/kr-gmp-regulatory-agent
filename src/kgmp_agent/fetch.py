"""국가법령정보센터(law.go.kr)에서 법령·행정규칙 본문과 별표 파일을 내려받는다.

- 법령/행정규칙 본문: 사이트가 화면에 그리는 HTML 을 받아 조문 단위 JSON 으로 저장한다.
- 별표: HWP·PDF 원본을 그대로 저장한다 (파싱은 parse.py 에서 kordoc 으로).

공식 Open API(DRF)는 사용자 ID(OC)와 서버 IP 등록이 필요하므로, 이 데모는 공개 웹 화면을 쓴다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.law.go.kr"
UA = {"User-Agent": "Mozilla/5.0 (kgmp-agent demo)"}


def _get(url: str, *, method: str = "GET", data: dict | None = None, tries: int = 5) -> requests.Response:
    """law.go.kr 는 간헐적으로 연결을 끊으므로 재시도한다."""
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.request(method, url, data=data, headers=UA, timeout=120)
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # 연결 끊김, 5xx 등
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"요청 실패: {url} ({last})")


def _text(el) -> str:
    return re.sub(r"[ \t ]+", " ", el.get_text(" ", strip=True)).strip()


@dataclass
class Article:
    no: str        # "제38조" / "제38조의2"
    title: str     # "의약품등의 생산 관리의무 및 보고"
    text: str      # 조문 전체 (항·호·목 줄바꿈)
    chapter: str   # 소속 장·절 (있으면)


def _parse_articles(html: str) -> tuple[str, list[Article]]:
    """lsInfoR / admRulInfoR 본문 HTML → (시행정보, 조문 목록)."""
    soup = BeautifulSoup(html, "html.parser")
    sub = soup.select_one(".subtit1")
    meta = _text(sub) if sub else ""
    if not meta:  # 법령 화면은 시행정보를 다른 요소에 둔다
        mm = re.search(r"\[시행[^\]]*\]\s*\[[^\]]*\]", soup.get_text(" ", strip=True))
        meta = mm.group(0) if mm else ""
    arts: list[Article] = []
    chapter = ""
    for g in soup.select("div.pgroup"):
        con = g.select_one("div.lawcon") or g
        lines = [_text(p) for p in con.find_all("p")]
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        first = lines[0]
        m = re.match(r"(제\d+조(?:의\d+)?)\s*\(([^)]*)\)", first)
        if not m:
            # "제1장 총칙" 같은 장·절 제목
            if re.match(r"제\d+(장|절|관)", first):
                chapter = first
            continue
        arts.append(Article(no=m.group(1), title=m.group(2), text="\n".join(lines), chapter=chapter))
    return meta, arts


def _appendix_links(html: str) -> list[dict]:
    """본문 HTML 에서 별표·서식 파일 링크(HWP/PDF)를 찾는다."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for a in soup.find_all("a", href=re.compile(r"flDownload\.do")):
        img = a.find("img")
        alt = (img.get("alt") if img else "") or ""
        fmt = "hwp" if "HWP" in alt.upper() else "pdf" if "PDF" in alt.upper() else None
        if fmt is None:
            continue
        # 같은 li 안의 제목 텍스트
        li = a.find_parent("li")
        title = ""
        if li:
            for t in li.find_all("a"):
                s = _text(t)
                if s.startswith("[별표") or s.startswith("[별지") or s.startswith("[별첨"):
                    title = s
                    break
        if not title:
            nm = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query).get("flNm", [""])[0]
            title = nm
        out.append({"title": title, "format": fmt, "url": urllib.parse.urljoin(BASE + "/LSW/", a["href"])})
    # 팝업 레이어에도 같은 링크가 반복되므로 (제목, 형식) 기준으로 첫 링크만 남긴다
    seen: set[tuple[str, str]] = set()
    uniq = []
    for o in out:
        k = (o["title"], o["format"])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq


def fetch_law(law_name: str) -> dict:
    """법령명으로 현행 법령 본문을 받는다."""
    wrap = _get(f"{BASE}/법령/{urllib.parse.quote(law_name)}").text
    m = re.search(r"lsiSeq=(\d+)", wrap)
    if not m:
        raise RuntimeError(f"lsiSeq 를 찾지 못함: {law_name}")
    lsi = m.group(1)
    # 시행일자(efYd)는 서버 기준(KST) 날짜여야 하므로 화면이 넘겨주는 값을 그대로 쓴다
    ef = re.search(r"efYd=(\d{8})", wrap)
    ef_yd = ef.group(1) if ef else (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y%m%d")
    body = _get(f"{BASE}/LSW/lsInfoR.do", method="POST",
                data={"lsiSeq": lsi, "chrClsCd": "010202", "efYd": ef_yd, "ancYnChk": "0"}).text
    meta, arts = _parse_articles(body)
    return {"source_url": f"{BASE}/법령/{law_name}", "lsiSeq": lsi, "meta": meta,
            "articles": [a.__dict__ for a in arts], "appendix_links": _appendix_links(body)}


def fetch_admrul(rule_name: str) -> dict:
    """행정규칙명으로 현행 고시 본문을 받는다."""
    wrap = _get(f"{BASE}/행정규칙/{urllib.parse.quote(rule_name)}").text
    m = re.search(r"admRulSeq=(\d+)", wrap)
    if not m:
        raise RuntimeError(f"admRulSeq 를 찾지 못함: {rule_name}")
    seq = m.group(1)
    page = _get(f"{BASE}/LSW/admRulInfoP.do?admRulSeq={seq}&chrClsCd=010201").text
    pm = re.search(r"var param = \{(.*?)\};", page, re.S)
    params = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', pm.group(1))) if pm else {"admRulSeq": seq}
    params.setdefault("admRulSeq", seq)
    body = _get(f"{BASE}/LSW/admRulInfoR.do", method="POST", data=params).text
    meta, arts = _parse_articles(body)
    return {"source_url": f"{BASE}/행정규칙/{rule_name}", "admRulSeq": seq, "meta": meta,
            "articles": [a.__dict__ for a in arts], "appendix_links": _appendix_links(body)}


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_get(url).content)
    return dest


def safe_name(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", s).strip("_")[:80]


def fetch_all(manifest: dict, raw_dir: Path, log=print) -> list[dict]:
    """manifest 의 모든 문서를 받는다. 결과 목록(문서별 저장 위치)을 돌려준다."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    bodies: dict[str, dict] = {}
    results: list[dict] = []
    for doc in manifest["documents"]:
        if doc["kind"] in ("law", "admrul"):
            log(f"[본문] {doc['title']}")
            data = fetch_law(doc["law_name"]) if doc["kind"] == "law" else fetch_admrul(doc["rule_name"])
            data.update({"id": doc["id"], "title": doc["title"], "tier": doc["tier"], "kind": doc["kind"]})
            out = raw_dir / "bodies" / f"{doc['id']}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            bodies[doc["id"]] = data
            results.append({"id": doc["id"], "path": str(out), "articles": len(data["articles"])})
    for doc in manifest["documents"]:
        if doc["kind"] != "appendix":
            continue
        parent = bodies[doc["parent"]]
        links = [l for l in parent["appendix_links"] if l["title"].startswith(doc["appendix_title"])]
        if not links:
            log(f"  ! 별표 링크 없음: {doc['appendix_title']}")
            continue
        firsts: dict[str, dict] = {}
        for l in links:  # 형식별 첫 링크만 (팝업 레이어의 중복 링크 제외)
            firsts.setdefault(l["format"], l)
        for l in firsts.values():
            dest = raw_dir / "appendix" / f"{doc['id']}.{l['format']}"
            log(f"[별표] {doc['title']} ({l['format']})")
            download(l["url"], dest)
            results.append({"id": doc["id"], "path": str(dest), "format": l["format"]})
    return results
