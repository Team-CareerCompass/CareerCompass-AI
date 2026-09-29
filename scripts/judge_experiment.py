"""LLM 검증자(judge)를 붙일 값어치가 있는지 **먼저 잰다** (#29).

규칙이 못 잡는 한국어 날조가 두 종 남아 있다 — 한글 표기 기술명(「타입스크립트」)과 공고
키워드를 경험처럼 쓰는 것(「웹 접근성을 고려한 디자인을 추가했어요」). 의미 판단이라 문자열로는
원리적으로 못 잡는다. LLM 판정이 맞는 도구지만, **판정자가 쓸 만한지부터 재야 한다** —
생성에서 지시를 잘 못 따르는 모델(DASH-002 는 casual 톤을 14건 중 2건만 지켰다)이 판정은
잘할 거라는 근거가 없다.

시험 데이터는 이미 있다. `eval/*draft-*.json` 에 기록된 답과 픽스처의 `forbidden` 라벨.

    양성(위반 있음)  = 그 답에 `forbidden` 문자열이 들어 있다
    음성(위반 없음)  = 들어 있지 않다 (라벨이 완전하지 않으므로 「알려진 위반이 없다」는 뜻)

재는 것: **양성 재현율**(잡아야 할 것을 잡나)과 **음성 오탐률**(멀쩡한 답에 밑줄을 긋나).
오탐률이 높으면 붙이지 않는다 — 가드의 오탐은 사용자가 쓴 진짜 문장을 지운다.

    CC_STUB_MODE=false python scripts/judge_experiment.py --model HCX-005 --limit 24 --save
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.budget import Budget
from app.config import settings
from app.draft_eval import forbidden_hits, load_all
from app.gateway import PAST_EXCERPT_PREFIX
from app.guard import extract_json
from app.prompts import load_prompt
from app.providers.base import ProviderError, complete_with_retry, cost_krw
from app.providers.hcx import HcxProvider

ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = ROOT / "eval"


def _cases() -> dict[str, Any]:
    return {c.id: c for c in load_all()}


def collect_answers() -> list[dict[str, Any]]:
    """기록된 초안 답 + 그 항목의 근거·금지어. 같은 답이 여러 번 기록됐으면 한 번만."""
    cases = _cases()
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for path in sorted(EVAL_ROOT.glob("*draft-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["rows"]:
            case = cases.get(row["id"])
            if case is None:
                continue
            answers = [(t, r["answer"]) for t, r in row["tones"].items()]
            if row.get("regen"):
                answers.append(("regen", row["regen"]["answer"]))
            if row.get("emphasis"):
                answers.append(("emphasis", row["emphasis"]["rotatedAnswer"]))
            for tone, answer in answers:
                if not answer or answer in seen:
                    continue
                seen.add(answer)
                cards = [
                    x
                    for x in case.request.experience_summaries
                    if not x.startswith(PAST_EXCERPT_PREFIX)
                ]
                hits = forbidden_hits(answer, case.forbidden)
                rows.append(
                    {
                        "id": case.id,
                        "tone": tone,
                        "answer": answer,
                        "evidence": cards,
                        "postingTitle": case.request.posting_title,
                        "question": case.request.question,
                        "known": hits,  # 비어 있으면 「알려진 위반 없음」
                    }
                )
    return rows


def sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """양성을 전부 넣고 나머지를 음성으로 채운다. 순서는 결정적이다(id, tone)."""
    key = lambda r: (r["id"], r["tone"], r["answer"][:20])  # noqa: E731
    # 같은 위반이 여러 실행에 남아 있다 — 007 의 「접근성」이 8건이다. 종류마다 2건까지만 넣는다.
    # 안 그러면 재현율이 한 사례의 성패로 정해진다.
    per_kind: dict[tuple[str, str], int] = {}
    positives = []
    for r in sorted([r for r in rows if r["known"]], key=key):
        kind = (r["id"], r["known"][0])
        if per_kind.get(kind, 0) >= 2:
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        positives.append(r)
    negatives = sorted([r for r in rows if not r["known"]], key=key)
    take = max(0, limit - len(positives))
    step = max(1, len(negatives) // take) if take else 1
    return positives + negatives[::step][:take]


_PROMPT_VERSION: str | None = None


async def judge(provider: HcxProvider, row: dict[str, Any], budget: Budget) -> dict[str, Any]:
    prompt = load_prompt("verify_draft", _PROMPT_VERSION)
    evidence = "\n".join(f"- {x}" for x in row["evidence"]) or "(없음)"
    user = prompt.render(
        posting_title=row["postingTitle"],
        question=row["question"],
        evidence=evidence,
        draft=row["answer"],
    )
    budget.check(
        (len(prompt.system) + len(user)) * provider.price_in_krw + 200 * provider.price_out_krw
    )
    completion = await complete_with_retry(
        provider, prompt.system, user, max_tokens=200, temperature=0.0
    )
    budget.record(cost_krw(provider, completion))
    try:
        flagged = extract_json(completion.text).get("unsupported") or []
    except ProviderError:
        flagged = None  # 형식 위반 — 판정 실패
    return {
        "flagged": flagged if flagged is None else [str(x) for x in flagged],
        "costKrw": round(cost_krw(provider, completion), 4),
        "latencyMs": completion.latency_ms,
        "totalTokens": completion.total_tokens,
    }


def caught(flagged: list[str], known: list[str], answer: str) -> bool:
    """판정이 **그 위반을** 짚었는가. 아무 데나 밑줄 긋고 맞혔다고 하지 않는다."""
    joined = "".join(flagged).replace(" ", "").lower()
    return any(k.replace(" ", "").lower() in joined for k in known)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="HCX-005")
    parser.add_argument("--prompt", default=None, help="verify_draft 프롬프트 버전 (기본: 최신)")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    global _PROMPT_VERSION
    _PROMPT_VERSION = args.prompt
    rows = sample(collect_answers(), args.limit)
    provider = HcxProvider(settings.hcx_api_key, args.model, timeout_s=settings.llm_timeout_s)
    budget = Budget(
        settings.budget_ledger,
        daily_krw=settings.budget_daily_krw,
        monthly_krw=settings.budget_monthly_krw,
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        out = asyncio.run(judge(provider, row, budget))
        results.append({**row, **out})

    positives = [r for r in results if r["known"]]
    negatives = [r for r in results if not r["known"]]
    hit = [r for r in positives if r["flagged"] and caught(r["flagged"], r["known"], r["answer"])]
    fp = [r for r in negatives if r["flagged"]]
    broken = [r for r in results if r["flagged"] is None]

    print(
        f"모델 {args.model} · 판정 {len(results)}건 (양성 {len(positives)} · 음성 {len(negatives)})"
    )
    print("-" * 96)
    for r in results:
        mark = "?" if r["flagged"] is None else ("O" if r["flagged"] else ".")
        if r["known"]:
            mark = "O" if r in hit else "X"
        print(f"{r['id']} {r['tone']:<9} {mark} 알려진위반={r['known']} 판정={r['flagged']}")
    print("-" * 96)
    print(
        f"양성 재현율   {len(hit)}/{len(positives)}"
        + (f" ({len(hit) / len(positives):.0%})" if positives else "")
    )
    print(
        f"음성 오탐률   {len(fp)}/{len(negatives)}"
        + (f" ({len(fp) / len(negatives):.0%})" if negatives else "")
    )
    print(f"형식 실패     {len(broken)}")
    total = sum(r["costKrw"] for r in results)
    print(
        f"        비용 {total:.2f}원 (건당 {total / len(results):.2f}원) · "
        f"지연 p50 {sorted(r['latencyMs'] for r in results)[len(results) // 2]}ms"
    )

    if args.save:
        EVAL_ROOT.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        version = load_prompt("verify_draft", args.prompt).version
        out_path = EVAL_ROOT / f"{stamp}_judge-{version}-{args.model}.json"
        out_path.write_text(
            json.dumps(
                {
                    "ranAt": stamp,
                    "model": args.model,
                    "promptVersion": load_prompt("verify_draft", args.prompt).version,
                    "positives": len(positives),
                    "negatives": len(negatives),
                    "recall": len(hit) / len(positives) if positives else None,
                    "falsePositiveRate": len(fp) / len(negatives) if negatives else None,
                    "formatFailures": len(broken),
                    "costKrw": round(total, 4),
                    "rows": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n기록: {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
