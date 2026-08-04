"""Deterministic formal-delivery contract compiled from a confirmed Work task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any


_DELIVERABLE_WORK_TYPE_BY_TASK_TYPE = {
    "image": "poster",
    "video": "video",
    "presentation": "presentation",
    "document": "report",
}
_ALLOWED_ASPECT_RATIOS = frozenset({"1:1", "3:4", "9:16", "16:9"})
_ASPECT_RATIO_RE = re.compile(
    r"(?<!\d)(1|3|9|16)\s*[:：]\s*(1|4|9|16)(?!\d)"
)
_POSTER_COPY_RE = re.compile(
    r"^(?:[-*•]\s*)?(主标题|副标题|标语|CTA)\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_POSTER_COPY_LABELS = ("主标题", "副标题", "标语", "CTA")
_INLINE_POSTER_COPY_PATTERNS = {
    "主标题": re.compile(
        r"(?:主标题|大标题)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|\"([^\"\n]+)\")",
        re.IGNORECASE,
    ),
    "副标题": re.compile(
        r"副标题[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|\"([^\"\n]+)\")",
        re.IGNORECASE,
    ),
    "标语": re.compile(
        r"(?:标语|口号)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|\"([^\"\n]+)\")",
        re.IGNORECASE,
    ),
    "CTA": re.compile(
        r"(?:CTA|按钮(?:内)?(?:白色)?文字)[^，；。\n]{0,48}?(?:【([^】\n]+)】|「([^」\n]+)」|“([^”\n]+)”|\"([^\"\n]+)\")",
        re.IGNORECASE,
    ),
}
_COPY_WRAPPERS = {"【": "】", "「": "」", "“": "”", '"': '"'}


@dataclass(frozen=True, slots=True)
class WorkTaskDeliverableContract:
    """Server-owned values that a linked Deliverable request must preserve."""

    work_type: str
    goal: str
    spec: dict[str, str | int]


def _explicit_aspect_ratio(goal: str) -> str | None:
    matches = {
        f"{match.group(1)}:{match.group(2)}"
        for match in _ASPECT_RATIO_RE.finditer(goal)
        if f"{match.group(1)}:{match.group(2)}" in _ALLOWED_ASPECT_RATIOS
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _explicit_poster_copy(goal: str) -> str | None:
    values: dict[str, str] = {}

    def record(label: str, raw_value: str) -> bool:
        value = raw_value.strip()
        if len(value) >= 2 and _COPY_WRAPPERS.get(value[0]) == value[-1]:
            value = value[1:-1].strip()
        previous = values.get(label)
        if not value or (previous is not None and previous != value):
            return False
        values[label] = value
        return True

    for line in goal.splitlines():
        match = _POSTER_COPY_RE.match(line.strip())
        if match is None:
            continue
        raw_label, raw_value = match.groups()
        label = "CTA" if raw_label.upper() == "CTA" else raw_label
        if not record(label, raw_value):
            return None

    for label, pattern in _INLINE_POSTER_COPY_PATTERNS.items():
        for match in pattern.finditer(goal):
            raw_value = next(
                (candidate for candidate in match.groups() if candidate is not None),
                "",
            )
            if not record(label, raw_value):
                return None

    ordered = [values[label] for label in _POSTER_COPY_LABELS if label in values]
    if "主标题" not in values or len(ordered) < 3:
        return None
    if "CTA" in values and len(ordered) < 4:
        return None
    return "\n".join(ordered)


def work_task_deliverable_contract(task: Any) -> WorkTaskDeliverableContract | None:
    """Compile only explicit, unambiguous values from a task-only Work contract."""

    statement = getattr(task, "work_statement", None)
    if not isinstance(statement, Mapping):
        return None
    if str(statement.get("delivery_mode") or "").strip() != "task_only":
        return None

    source_work_type = str(
        statement.get("work_type") or getattr(task, "work_type", "") or ""
    ).strip()
    work_type = _DELIVERABLE_WORK_TYPE_BY_TASK_TYPE.get(source_work_type)
    if work_type is None:
        return None

    goal = str(statement.get("objective") or getattr(task, "intent", "") or "").strip()
    if not goal:
        return None

    spec: dict[str, str | int] = {}
    if work_type in {"poster", "video"}:
        aspect_ratio = _explicit_aspect_ratio(goal)
        if aspect_ratio is not None:
            spec["aspect_ratio"] = aspect_ratio
    if work_type == "poster":
        exact_copy = _explicit_poster_copy(goal)
        if exact_copy is not None:
            spec["exact_copy"] = exact_copy

    return WorkTaskDeliverableContract(
        work_type=work_type,
        goal=goal,
        spec=spec,
    )


__all__ = ["WorkTaskDeliverableContract", "work_task_deliverable_contract"]
