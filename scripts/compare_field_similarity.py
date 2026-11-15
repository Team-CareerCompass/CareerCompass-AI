"""분야 유사도: BE 부분일치 vs HCX 임베딩 (보고서 4.4.4) — 공고 35건, 관심 분야 12개.

    python scripts/compare_field_similarity.py              # 임베딩까지. 처음 ≈1원, 그 뒤 캐시
    python scripts/compare_field_similarity.py --no-embed   # BE 부분일치만 (0원)
    python scripts/compare_field_similarity.py --save       # eval/ 에 결과 기록

정답은 `fixtures/similarity/profiles.json`. 공고 키워드는 두 가지로 잰다 — **정답 키워드**(방식만
비교)와 **실제 파싱 키워드**(11-15 HCX-005 파싱 기록, 실서버에서 BE 가 받는 것).
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.budget import Budget, BudgetExceeded
from app.config import settings
from app.embedding import HcxEmbedder, cosine
from app.field_similarity import keyword_score, posting_text, profile_text
from app.fixtures import Fixture, load_all
from app.providers.base import RateLimited, TransientError
from app.similarity_eval import (
    auc,
    bootstrap_macro_auc_diff,
    interest_texts,
    macro_auc,
    mean,
    r_precision,
)

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "fixtures" / "similarity" / "profiles.json"
PARSE_RECORD = ROOT / "eval" / "2026-11-15T042034Z_llm-flat-v2-HCX-005.json"
EVAL_ROOT = ROOT / "eval"
CACHE_DIR = ROOT / ".cache" / "embeddings"

Scores = dict[str, list[float]]


def posting_set(excluded: dict[str, str]) -> list[Fixture]:
    return [fx for fx in load_all() if fx.expected.get("keywords") and fx.id not in excluded]


def parsed_keywords() -> dict[str, list[str]]:
    record = json.loads(PARSE_RECORD.read_text(encoding="utf-8"))
    return {row["id"]: row.get("keywords") or [] for row in record["rows"]}


async def embed_all(embedder: HcxEmbedder, texts: list[str]) -> dict[str, list[float]]:
    """순서대로 부른다 — 테스트 키 호출 한도가 있고, 장부를 프로세스 하나가 쓰게."""
    vectors: dict[str, list[float]] = {}
    for text in dict.fromkeys(t.strip() for t in texts if t.strip()):
        for attempt in range(6):
            try:
                vectors[text] = (await embedder.embed(text)).vector
                break
            except RateLimited:
                await asyncio.sleep(2.0 * (attempt + 1))
            except TransientError:
                if attempt >= 1:
                    raise
                await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"임베딩 429 가 계속된다: {text[:40]}")
    return vectors


Labels = dict[str, list[bool]]


def grade_labels(profiles: list[dict[str, Any]], ids: list[str], *, strict: bool) -> Labels:
    floor = 2 if strict else 1
    return {p["id"]: [p["relevant"].get(pid, 0) >= floor for pid in ids] for p in profiles}


def summarize(name: str, scores: Scores, labels: Labels, strict: Labels) -> dict[str, Any]:
    per_profile = {p: auc(scores[p], labels[p]) for p in labels}
    rprec = [v for p in labels if (v := r_precision(scores[p], labels[p])) is not None]
    pooled_scores = [s for p in labels for s in scores[p]]
    pooled_labels = [y for p in labels for y in labels[p]]
    return {
        "method": name,
        "macroAuc": macro_auc(scores, labels),
        "macroAucStrict": macro_auc(scores, strict),
        "pooledAuc": auc(pooled_scores, pooled_labels),
        "macroRPrecision": mean(rprec),
        "perProfileAuc": per_profile,
    }


def contains_errors(scores: Scores, labels: Labels) -> dict[str, Any]:
    pos = [(s, y) for p in labels for s, y in zip(scores[p], labels[p], strict=True) if y]
    neg = [(s, y) for p in labels for s, y in zip(scores[p], labels[p], strict=True) if not y]
    return {
        "relevantPairs": len(pos),
        "relevantScoredZero": sum(1 for s, _ in pos if s == 0),
        "irrelevantPairs": len(neg),
        "irrelevantScoredAboveZero": sum(1 for s, _ in neg if s > 0),
    }


def top(scores: list[float], ids: list[str], k: int = 3) -> list[str]:
    return [pid for _, pid in sorted(zip(scores, ids, strict=True), key=lambda t: -t[0])[:k]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-embed", action="store_true", help="BE 부분일치만 (0원)")
    parser.add_argument("--save", action="store_true", help="eval/ 에 결과 기록")
    args = parser.parse_args()

    spec = json.loads(PROFILES.read_text(encoding="utf-8"))
    profiles: list[dict[str, Any]] = spec["profiles"]
    postings = posting_set(spec["excluded"])
    ids = [fx.id for fx in postings]
    parsed = parsed_keywords()
    gold_kw = {fx.id: list(fx.expected["keywords"]) for fx in postings}
    real_kw = {fx.id: parsed.get(fx.id, []) for fx in postings}
    titles = {fx.id: fx.title for fx in postings}

    def interests(p: dict[str, Any], *, tags: bool) -> list[str]:
        return interest_texts(p["jobCodes"], p["tags"] if tags else [], spec["jobLabels"])

    labels = grade_labels(profiles, ids, strict=False)
    strict = grade_labels(profiles, ids, strict=True)

    def be(keywords: dict[str, list[str]], *, tags: bool) -> Scores:
        return {
            p["id"]: [
                float(keyword_score(keywords[pid], interests(p, tags=tags))[0]) for pid in ids
            ]
            for p in profiles
        }

    methods: dict[str, Scores] = {
        "BE 부분일치 · 정답 키워드": be(gold_kw, tags=True),
        "BE 부분일치 · 파싱 키워드": be(real_kw, tags=True),
        "BE 부분일치 · 정답 키워드 · 태그 없음": be(gold_kw, tags=False),
    }

    spent = 0.0
    calls = 0
    if not args.no_embed:
        budget = Budget(
            settings.budget_ledger,
            daily_krw=settings.budget_daily_krw,
            monthly_krw=settings.budget_monthly_krw,
        )
        embedder = HcxEmbedder(settings.hcx_api_key, budget=budget, cache_dir=CACHE_DIR)

        # 서비스(`/v1/field-similarity`)와 같은 텍스트 모양 — 여기서 잰 숫자가 그 API 의 숫자다
        def profile_of(p: dict[str, Any], *, tags: bool) -> str:
            return profile_text(interests(p, tags=tags))

        def posting_of(pid: str, keywords: dict[str, list[str]], *, title: bool) -> str:
            if title:
                return posting_text(titles[pid], keywords[pid])
            return ", ".join(keywords[pid])

        variants: dict[str, tuple[dict[str, list[str]], bool, bool]] = {
            "임베딩 · 정답 키워드": (gold_kw, False, True),
            "임베딩 · 제목+정답 키워드": (gold_kw, True, True),
            "임베딩 · 제목+파싱 키워드": (real_kw, True, True),
            "임베딩 · 제목+정답 키워드 · 태그 없음": (gold_kw, True, False),
        }
        texts = [profile_of(p, tags=t) for p in profiles for t in (True, False)]
        for keywords, title, _ in variants.values():
            texts += [posting_of(pid, keywords, title=title) for pid in ids]
        try:
            vectors = asyncio.run(embed_all(embedder, texts))
        except BudgetExceeded as exc:
            print(f"예산에 막혔다. 임베딩 없이 끝낸다: {exc}")
            return 2
        spent, calls = embedder.spent_krw, embedder.calls

        def embedding_scores(keywords: dict[str, list[str]], title: bool, tags: bool) -> Scores:
            out: Scores = {}
            for p in profiles:
                pv = vectors[profile_of(p, tags=tags)]
                row: list[float] = []
                for pid in ids:
                    text = posting_of(pid, keywords, title=title).strip()
                    row.append(cosine(pv, vectors[text]) if text else -1.0)
                out[p["id"]] = row
            return out

        for name, (keywords, title, tags) in variants.items():
            methods[name] = embedding_scores(keywords, title, tags)

    summaries = [summarize(name, s, labels, strict) for name, s in methods.items()]
    n_pairs = sum(map(sum, labels.values()))
    print(f"공고 {len(ids)}건, 프로필 {len(profiles)}개, 관련 쌍 {n_pairs}")
    print(f"{'방식':<34} {'평균AUC':>7} {'엄격':>6} {'통합AUC':>7} {'R-정밀':>6}")
    for s in summaries:
        print(
            f"{s['method']:<34} {s['macroAuc']:>7.3f} {s['macroAucStrict']:>6.3f} "
            f"{s['pooledAuc']:>7.3f} {s['macroRPrecision']:>6.3f}"
        )

    base_name = "BE 부분일치 · 정답 키워드"
    errors = contains_errors(methods[base_name], labels)
    print(
        f"\nBE 부분일치(정답 키워드): 관련 쌍 {errors['relevantPairs']}개 중 0점 "
        f"{errors['relevantScoredZero']}개 · 무관 쌍 {errors['irrelevantPairs']}개 중 0점 초과 "
        f"{errors['irrelevantScoredAboveZero']}개"
    )

    comparisons: dict[str, Any] = {}
    pairs: list[tuple[str, str]] = [
        (base_name, "임베딩 · 정답 키워드"),
        (base_name, "임베딩 · 제목+정답 키워드"),
        ("BE 부분일치 · 파싱 키워드", "임베딩 · 제목+파싱 키워드"),
    ]
    for a, b in pairs:
        if b not in methods:
            continue
        lo, hi = bootstrap_macro_auc_diff(methods[a], methods[b], labels)
        diff = macro_auc(methods[b], labels) - macro_auc(methods[a], labels)
        comparisons[f"{b} - {a}"] = {"diff": diff, "ci95": [lo, hi]}
        print(f"평균 AUC 차이 [{b}] - [{a}]: {diff:+.3f} (95% 구간 {lo:+.3f} ~ {hi:+.3f})")

    print("\n프로필별 AUC")
    names = list(methods)
    for p in profiles:
        pid = p["id"]
        n_rel = sum(labels[pid])
        cells = " ".join(
            f"{v:.2f}" if (v := summaries[i]["perProfileAuc"][pid]) is not None else "  - "
            for i in range(len(names))
        )
        print(f"  {pid:<10} 관련 {n_rel}  {cells}")
    print("  (열 순서: " + " | ".join(names) + ")")

    shown = {base_name, "임베딩 · 제목+정답 키워드"}
    tops = {
        p["id"]: {n: top(methods[n][p["id"]], ids) for n in names if n in shown} for p in profiles
    }

    if calls or spent:
        print(f"\n임베딩 호출 {calls}번 · {spent:.2f}원 (캐시 히트는 0원)")

    if args.save:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        out = EVAL_ROOT / f"{stamp}_field-similarity.json"
        payload = {
            "ranAt": stamp,
            "postings": ids,
            "profiles": [p["id"] for p in profiles],
            "parseRecord": PARSE_RECORD.name,
            "embedding": {"model": "embedding-v2", "calls": calls, "costKrw": round(spent, 4)},
            "summaries": summaries,
            "beContainsErrors": errors,
            "comparisons": comparisons,
            "top3": tops,
            "scores": methods,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"기록: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
