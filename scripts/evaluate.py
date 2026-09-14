"""평가셋 러너 CLI (#5).

    python scripts/evaluate.py                    # 규칙 전용, 표만
    python scripts/evaluate.py --save             # eval/ 에 결과 기록
    python scripts/evaluate.py --pipeline llm     # 게이트웨이(규칙+LLM). 모델을 부른다
    CC_LLM_CACHE=record python scripts/evaluate.py --pipeline llm --save   # 녹화하며

**규칙이나 프롬프트를 고치면 이것을 돌린다.** 결과를 저장소에 남겨 두면
「지난주보다 좋아졌나」를 커밋 로그로 답할 수 있다.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluation import evaluate

EVAL_ROOT = Path(__file__).resolve().parent.parent / "eval"

_MARKS = {
    "correct": "O 맞음",
    "correct_null": "O null정답",
    "wrong": "X 틀림",
    "missed": "- 못읽음",
    "hallucinated": "XX 날조",
    "skip": ". 해당없음",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="eval/ 에 결과를 기록한다")
    parser.add_argument("--pipeline", choices=["rules", "llm"], default="rules")
    args = parser.parse_args()

    report = evaluate(pipeline=args.pipeline)
    if not report.rows:
        print("픽스처가 없다. fixtures/postings/ 를 채운다.")
        return 1

    header = f"파이프라인: {report.pipeline}"
    if report.prompt_version:
        header += f" · 프롬프트 {report.prompt_version} · 모델 {report.model}"
    print(header)
    print(
        f"{'id':<5} {'유형':<12} {'마감일':<12} {'판정':<14} {'문항':>4} {'우대':>4}  근거 / 키워드"
    )
    print("-" * 100)
    for row in report.rows:
        inferred = "~" if row["yearInferred"] else " "
        tail = (row["dueDateRaw"] or "")[:34]
        if row.get("failReason"):
            tail = f"실패 {row['failReason']}"
        elif row.get("keywords"):
            kw = row.get("keywordGrade") or {}
            mark = f"[{kw.get('hit', 0)}/{kw.get('gold', 0)}] " if kw else ""
            tail = mark + ", ".join(row["keywords"])[:56]
        print(
            f"{row['id']:<5} {row['type']!s:<12} {row['dueDate']!s:<12}"
            f"{inferred}{_MARKS[row['dueGrade']]:<13} {len(row['formQuestions']):>4} "
            f"{len(row['preferences']):>4}  {tail}"
        )

    print("-" * 100)
    for label, accuracy, counter in (
        ("마감일", report.due_accuracy, report.due_date),
        ("유형  ", report.type_accuracy, report.type),
        ("문항  ", report.question_accuracy, report.form_questions),
        ("공고판별", report.posting_accuracy, report.posting),
    ):
        total = sum(v for k, v in counter.items() if k != "skip")
        rate = f"({accuracy:.0%})" if accuracy is not None else ""
        hits = counter["correct"] + counter.get("correct_null", 0)
        extra = ""
        if counter.get("false_positive") or counter.get("false_negative"):
            fp, fn = counter.get("false_positive", 0), counter.get("false_negative", 0)
            extra = f"  · 공고오인 {fp} · 공고누락 {fn}"
        print(f"{label}  정답 {hits}/{total} {rate}{extra}")

    print(
        f"        마감일 틀림 {report.due_date['wrong']} · "
        f"못읽음 {report.due_date['missed']} · **날조 {report.hallucinated}**"
    )
    for label, recall, precision, counter in (
        ("키워드", report.keyword_recall, report.keyword_precision, report.keywords),
        ("우대  ", report.preference_recall, report.preference_precision, report.preferences),
    ):
        if counter["gold"] == 0 and counter["pred"] == 0:
            continue
        r = f"{recall:.0%}" if recall is not None else "-"
        p_ = f"{precision:.0%}" if precision is not None else "-"
        print(
            f"{label}  재현율 {counter['hit']}/{counter['gold']} ({r}) · "
            f"정밀도 {counter['predHit']}/{counter['pred']} ({p_})"
            + (f" · **금지어 {counter['forbidden']}**" if counter["forbidden"] else "")
        )

    if report.pipeline == "llm":
        # 규칙이 먼저 거른 것(EMPTY·NOT_A_POSTING)은 토큰 0 — 호출이 아니다
        usages = [r["usage"] for r in report.rows if r.get("usage") and r["usage"]["totalTokens"]]
        tokens = sum(u["totalTokens"] for u in usages)
        cost = sum(u["costKrw"] for u in usages)
        ms = [u["latencyMs"] for u in usages if u["latencyMs"]]
        p50 = sorted(ms)[len(ms) // 2] if ms else 0
        print(
            f"        LLM 호출 {len(usages)}건 · 토큰 {tokens:,} · "
            f"비용 {cost:.2f}원 · 지연 p50 {p50}ms"
        )

    if args.save:
        EVAL_ROOT.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        tag = report.pipeline
        if report.prompt_version:
            tag += f"-{report.prompt_version}-{report.model}"
        out = EVAL_ROOT / f"{stamp}_{tag}.json"
        payload = {"ranAt": stamp, **report.as_dict()}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n기록: {out.relative_to(EVAL_ROOT.parent)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
