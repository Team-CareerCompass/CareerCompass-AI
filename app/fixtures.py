"""`fixtures/postings/` 로더.

머리말은 사람이 손으로 쓰는 것이라 YAML 파서를 붙이지 않는다 — `key: value` 뿐이다.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "postings"

_FRONT_MATTER = re.compile(r"\A---\n(?P<head>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_LIST_VALUE = re.compile(r"\A\[(.*)\]\Z")


@dataclass
class Fixture:
    id: str
    title: str
    body: str
    path: Path
    type: str | None = None
    source: str | None = None
    url: str | None = None
    collected_at: date | None = None
    images: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)

    @property
    def image_only(self) -> bool:
        return bool(self.images) and len(self.body.strip()) < 200


def _parse_head(head: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in head.split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not value:
            continue
        if (m := _LIST_VALUE.match(value)) is not None:
            out[key] = [v.strip() for v in m.group(1).split(",") if v.strip()]
        else:
            out[key] = value.strip('"').strip("'")
    return out


def load(path: Path) -> Fixture:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER.match(text)
    head = _parse_head(m.group("head")) if m else {}
    body = m.group("body") if m else text

    collected = head.get("collectedAt")
    expected_path = path.parent / "expected" / f"{path.stem}.json"

    return Fixture(
        id=str(head.get("id", path.stem)),
        title=str(head.get("title", path.stem)),
        body=body,
        path=path,
        type=head.get("type"),
        source=head.get("source"),
        url=head.get("url"),
        collected_at=date.fromisoformat(collected) if collected else None,
        images=head.get("images", []),
        expected=json.loads(expected_path.read_text(encoding="utf-8"))
        if expected_path.exists()
        else {},
    )


def load_all(root: Path = FIXTURE_ROOT, *, skip_example: bool = True) -> list[Fixture]:
    paths = sorted(root.glob("*.md"))
    return [
        load(p)
        for p in paths
        if p.name not in {"README.md"} and not (skip_example and p.stem.startswith("000"))
    ]
