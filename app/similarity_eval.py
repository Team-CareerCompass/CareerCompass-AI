"""분야 유사도 비교 (보고서 4.4.4) — 평가 전용. 서비스는 이 파일을 쓰지 않는다.

BE 글자 비교 이식본은 스텁 응답도 쓰므로 `app/field_similarity.py` 의 `keyword_score` 에 있다.
여기는 입력 만들기와 순위 지표다. 입력은 BE 가 만드는 모양 그대로다 —
`AnalysisService.interestTexts` 는 관심 직무마다 코드·라벨을 넣고 태그를 뒤에 붙인다.

**지표는 순위만 본다.** BE 는 0~100 정수(동점이 많다), 임베딩은 코사인이라 눈금이 다르다. 같은
문턱으로 자를 수 없으니 순위로 비교한다.

- AUC: 관련 공고 하나와 무관 공고 하나를 뽑았을 때 관련 쪽 점수가 더 높을 확률. 동점은 반으로 센다 —
  부분일치가 전부 0 이면 정확히 0.5(동전 던지기)다
- R-정밀도: 관련 공고가 R 개인 사람에게 점수 상위 R 개를 보여 줬을 때 그중 관련 비율. 동점 경계는
  기댓값으로 센다(동점을 무작위로 깨는 것과 같다)
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence


def interest_texts(
    job_codes: Sequence[str], tags: Sequence[str], job_labels: Mapping[str, str]
) -> list[str]:
    """BE `AnalysisService.interestTexts` 순서 그대로 — 직무마다 코드, 라벨, 그다음 태그."""
    texts: list[str] = []
    for code in job_codes:
        texts.append(code)
        if code in job_labels:
            texts.append(job_labels[code])
    texts.extend(tags)
    return texts


def auc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def r_precision(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    r = sum(labels)
    if r == 0:
        return None
    taken = 0
    hits = 0.0
    for level in sorted(set(scores), reverse=True):
        room = r - taken
        if room <= 0:
            break
        group = [y for s, y in zip(scores, labels, strict=True) if s == level]
        if len(group) <= room:
            hits += sum(group)
            taken += len(group)
        else:
            hits += room * sum(group) / len(group)
            taken += room
    return hits / r


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def macro_auc(
    scores: Mapping[str, Sequence[float]],
    labels: Mapping[str, Sequence[bool]],
    index: Sequence[int] | None = None,
) -> float:
    """프로필마다 AUC 를 내고 평균한다. 관련·무관이 둘 다 있는 프로필만 센다."""
    values: list[float] = []
    for profile, ys in labels.items():
        idx = index if index is not None else range(len(ys))
        value = auc([scores[profile][i] for i in idx], [ys[i] for i in idx])
        if value is not None:
            values.append(value)
    return mean(values)


def bootstrap_macro_auc_diff(
    base: Mapping[str, Sequence[float]],
    other: Mapping[str, Sequence[float]],
    labels: Mapping[str, Sequence[bool]],
    *,
    n: int = 2000,
    seed: int = 0,
) -> tuple[float, float]:
    """공고를 복원 추출해 `other - base` 평균 AUC 차이의 95% 구간을 낸다 (짝지은 비교)."""
    rng = random.Random(seed)
    size = len(next(iter(labels.values())))
    diffs: list[float] = []
    for _ in range(n):
        idx = [rng.randrange(size) for _ in range(size)]
        a = macro_auc(base, labels, idx)
        b = macro_auc(other, labels, idx)
        if not (math.isnan(a) or math.isnan(b)):
            diffs.append(b - a)
    diffs.sort()
    return diffs[int(0.025 * len(diffs))], diffs[math.ceil(0.975 * len(diffs)) - 1]
