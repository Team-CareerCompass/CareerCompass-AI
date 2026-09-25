"""초안 평가 러너 CLI (#16 #18 #19).

    CC_STUB_MODE=false python scripts/evaluate_drafts.py          # formal·casual + 재생성
    CC_STUB_MODE=false python scripts/evaluate_drafts.py --save   # eval/ 에 기록 (답 본문 포함)
    python scripts/evaluate_drafts.py --no-regen                  # 재생성 측정 생략
    python scripts/evaluate_drafts.py --tones formal casual confident

파싱 러너(`evaluate.py`)와 달리 정답이 없다 — 어긴 것을 센다. 지표 정의는 `app/draft_eval.py`.
**답 본문을 같이 기록한다.** 숫자만으로는 「왜 이 톤이 안 구별되나」를 못 본다.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.draft_eval import TONE_TARGET, evaluate_drafts

EVAL_ROOT = Path(__file__).resolve().parent.parent / "eval"


def _pct(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "-"


def _f2(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="eval/ 에 결과를 기록한다")
    parser.add_argument("--no-regen", action="store_true", help="재생성 겹침 측정을 생략한다")
    parser.add_argument(
        "--no-emphasis", action="store_true", help="강조 카드(순서 바꿔 한 번 더) 측정을 생략한다"
    )
    parser.add_argument(
        "--tones", nargs="+", default=["formal", "casual"], help="돌릴 톤. 첫 톤으로 재생성한다"
    )
    parser.add_argument("--answers", action="store_true", help="답 본문도 출력한다")
    parser.add_argument("--only", nargs="*", default=None, help="이 id 들만 (소표본 실험용)")
    args = parser.parse_args()

    cases = None
    if args.only:
        from app.draft_eval import load_all

        cases = [c for c in load_all() if c.id in set(args.only)]
    report = evaluate_drafts(
        cases,
        tones=tuple(args.tones),
        regen=not args.no_regen,
        emphasis=not args.no_emphasis,
    )
    if not report.rows:
        print("픽스처가 없다. fixtures/drafts/ 를 채운다.")
        return 1

    print(f"프롬프트 {report.prompt_version} · 모델 {report.model} · 톤 {', '.join(report.tones)}")
    print(
        f"{'id':<4} {'톤':<9} {'글자/상한':<11} {'길이':<6} {'어미':>5} {'fb':>3} {'금지':>3}"
        "  미검증 / 인용"
    )
    print("-" * 100)
    for row in report.rows:
        entries = [*row["tones"].values()]
        if row.get("regen"):
            entries.append({**row["regen"], "tone": f"{row['regen']['tone']}#2"})
        for r in entries:
            limit = r["limit"] or "-"
            fb = "F" if r["fallback"] else ""
            tail = ", ".join(r["unverified"]) or ""
            tail += f"  idx={r['usedIndexes']}"
            if r["forbiddenHits"]:
                tail += f"  금지={r['forbiddenHits']}"
            if "ngramJaccard" in r:
                tail += f"  겹침 3g={r['ngramJaccard']:.2f} 문장={r['sentenceOverlap']:.2f}"
            print(
                f"{row['id']:<4} {r['tone']:<9} {r['charCount']:>4}/{limit!s:<6} "
                f"{r['lengthGrade']:<6} {_pct(r['endingRatio']):>5} {fb:>3} "
                f"{len(r['forbiddenHits']):>3}  {tail}"
            )
            if args.answers:
                print(f"     └ {r['answer']}")
        if row.get("emphasis"):
            e = row["emphasis"]
            mark = "O" if e["baseReflectsFirst"] and e["rotatedReflectsFirst"] else "X"
            print(
                f"     강조 {mark} · 원래 순서 첫 카드 반영 {e['baseReflectsFirst']}"
                f" · 바꾼 순서 {e['rotatedReflectsFirst']}"
                f" · idx {e['baseUsedIndexes']} → {e['rotatedUsedIndexes']}"
            )
            if args.answers:
                print(f"     └ (순서 바꿈) {e['rotatedAnswer']}")
        if row.get("factJaccard") is not None:
            mark = "O" if row["distinguishable"] else "X"
            print(f"     톤 구별 {mark} · 사실 Jaccard {row['factJaccard']:.2f}")

    print("-" * 100)
    ok, total = report.distinguishable
    print(
        f"글자 수        상한 준수 {_pct(report.length_compliance)} · "
        f"70% 이상 채움 {_pct(report.fill_compliance)} (평균 {_pct(report.fill_ratio_mean)}) · "
        f"제한 없음 400~600 {_pct(report.default_range_compliance)}"
    )
    print(
        f"사실           fallback {_pct(report.fallback_rate)} · "
        f"남은 미검증 {report.unverified_total} · **금지 문자열 {report.forbidden_total}**"
    )
    cons = " · ".join(f"{t} {_pct(report.tone_consistency(t))}" for t in report.tones)
    print(f"톤 일관성      {cons}  (합격선 {TONE_TARGET:.0%})")
    if total:
        print(
            f"톤 구별        {ok}/{total} · 톤 간 사실 Jaccard 평균 {_f2(report.fact_jaccard_mean)}"
        )
    emph_ok, emph_total = report.emphasis_follows
    if emph_total:
        print(f"강조 카드     순서를 바꾸면 그 카드가 부각된 항목 {emph_ok}/{emph_total}")
    if report.regen:
        similar, n = report.regen_similar
        print(
            f"재생성 겹침    3-gram {_f2(report.regen_ngram_mean)} (≥0.5 인 것 {similar}/{n}) · "
            f"문장 {_f2(report.regen_sentence_mean)}"
            f"  (다른 항목끼리 바닥선 {_f2(report.cross_case_ngram_mean)})"
        )
    print(
        f"        LLM 호출 {report.calls}건 · 토큰 {report.total_tokens:,} · "
        f"비용 {report.cost_krw:.2f}원"
    )

    if args.save:
        EVAL_ROOT.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        out = EVAL_ROOT / f"{stamp}_draft-{report.prompt_version}-{report.model}.json"
        payload = {"ranAt": stamp, **report.as_dict()}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n기록: {out.relative_to(EVAL_ROOT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
