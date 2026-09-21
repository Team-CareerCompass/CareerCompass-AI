"""코멘트 평가 러너 CLI (#14).

    CC_STUB_MODE=false python scripts/evaluate_comments.py           # 모델을 부른다 (12건, ~2원)
    CC_STUB_MODE=false python scripts/evaluate_comments.py --save    # eval/ 에 기록 (문장 포함)
    python scripts/evaluate_comments.py --only 001 005

지표 정의는 `app/comment_eval.py`. **문장을 같이 기록한다** — 숫자만으로는 「왜 이게 날조인가」를
못 본다. 코멘트의 실패는 조용해서 눈으로 한 번은 봐야 한다.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.comment_eval import MAX_LINES, evaluate_comments, load_all

EVAL_ROOT = Path(__file__).resolve().parent.parent / "eval"


def _pct(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="eval/ 에 결과를 기록한다")
    parser.add_argument("--only", nargs="*", default=None, help="이 id 들만")
    args = parser.parse_args()

    cases = load_all()
    if args.only:
        cases = [c for c in cases if c.id in set(args.only)]
    if not cases:
        print("픽스처가 없다. fixtures/comments/ 를 채운다.")
        return 1

    report = evaluate_comments(cases)
    print(f"프롬프트 {report.prompt_version} · 모델 {report.model}")
    print("-" * 100)
    for row in report.rows:
        flags = []
        if row["forbiddenHits"]:
            flags.append(f"금지={row['forbiddenHits']}")
        if row["unverified"]:
            flags.append(f"미검증={row['unverified']}")
        if row["grounded"] is False:
            flags.append("근거 못 지목한 문장 있음")
        if row["lines"] > MAX_LINES or row["sentencesOver"]:
            flags.append(f"형식 {row['lines']}줄 {row['sentencesOver']}")
        if row["pii"]:
            flags.append("PII")
        flags += row["expectMisses"]
        mark = "X" if flags else "O"
        print(f"{row['id']} {mark} {'  '.join(flags)}")
        print(f"     강점: {row['strength']}")
        print(f"     약점: {row['weakness']}")

    print("-" * 100)
    ok, total = report.expect_ok
    print(f"기대 충족     {ok}/{total} (근거 있으면 쓰고, 없으면 null)")
    print(
        f"날조          **금지 문자열 {report.forbidden_total}** · "
        f"미검증 수치·영문 {report.unverified_total}"
    )
    print(
        f"근거 지목     문장 전부가 근거를 지목한 항목 {_pct(report.grounded_rate)} · "
        f"근거 있는데 침묵 {_pct(report.silent_rate)}"
    )
    print(f"형식          위반 {report.format_violations} · PII {report.pii_total}")
    print(f"        LLM 토큰 {report.total_tokens:,} · 비용 {report.cost_krw:.2f}원")

    if args.save:
        EVAL_ROOT.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        out = EVAL_ROOT / f"{stamp}_comments-{report.prompt_version}-{report.model}.json"
        out.write_text(
            json.dumps({"ranAt": stamp, **report.as_dict()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n기록: {out.relative_to(EVAL_ROOT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
