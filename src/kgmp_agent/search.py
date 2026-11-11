"""한국어 BM25 검색 (외부 임베딩 API 없이 동작).

형태소 분석기 없이 '어절 + 한글 2글자 n-gram'으로 토큰화한다. 법령 문장은 조사·어미가 붙어
어절이 자주 바뀌므로("수율관리기준을", "수율관리기준이란") 2-gram 이 재현율을 크게 올린다.
"""

from __future__ import annotations

import math
import re
from collections import Counter

HANGUL = re.compile(r"[가-힣]+")
TOKEN = re.compile(r"[가-힣]+|[A-Za-z]+|\d+(?:\.\d+)*")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for w in TOKEN.findall(text.lower()):
        if HANGUL.fullmatch(w):
            if len(w) == 1:
                toks.append(w)
            else:
                toks += [w[i:i + 2] for i in range(len(w) - 1)]
            if 2 <= len(w) <= 8:
                toks.append(f"#{w}")  # 어절 전체도 한 번 (정확 일치 가중)
        else:
            toks.append(w)
    return toks


class BM25:
    def __init__(self, docs: list[dict], k1: float = 1.4, b: float = 0.6):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf: list[Counter] = []
        df: Counter = Counter()
        for d in docs:
            # 위치 정보(인용 표기)도 검색 대상에 포함: "제조관리", "일탈" 같은 절 제목으로도 찾게
            c = Counter(tokenize(d["citation"] + "\n" + d["text"]))
            self.tf.append(c)
            df.update(c.keys())
        self.n = len(docs)
        self.avgdl = sum(sum(c.values()) for c in self.tf) / max(self.n, 1)
        self.idf = {t: math.log(1 + (self.n - v + 0.5) / (v + 0.5)) for t, v in df.items()}

    def search(self, query: str, k: int = 8, *, doc_ids: list[str] | None = None,
               max_tier: int | None = None) -> list[tuple[float, dict]]:
        q = Counter(tokenize(query))
        scored = []
        for i, (d, tf) in enumerate(zip(self.docs, self.tf)):
            if doc_ids and d["doc_id"] not in doc_ids:
                continue
            if max_tier and d["tier"] > max_tier:
                continue
            dl = sum(tf.values())
            s = 0.0
            for t, qn in q.items():
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]
