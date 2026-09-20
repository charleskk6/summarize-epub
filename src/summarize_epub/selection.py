from __future__ import annotations

from typing import Protocol, Sequence


class Selectable(Protocol):
    index: int
    title: str
    front_matter: bool


def parse_indices(expression: str, count: int) -> list[int]:
    if expression.strip().lower() == "all":
        return list(range(1, count + 1))
    result: set[int] = set()
    for part in expression.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Empty chapter selection")
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                a, b = int(start), int(end) if end.strip() else count
            else:
                a = b = int(part)
        except ValueError:
            raise ValueError(f"Invalid chapter range: {part!r}") from None
        if not 1 <= a <= b <= count:
            raise ValueError(f"Chapter range {part!r} is outside 1–{count}")
        result.update(range(a, b + 1))
    return sorted(result)


def resolve(chapters: Sequence[Selectable], indices: str | None = None,
            titles: Sequence[str] = (), all_chapters: bool = False,
            exclude: str | None = None, skip_front_matter: bool = True) -> list[int]:
    selected = set(parse_indices(indices, len(chapters))) if indices else set()
    if all_chapters:
        selected.update(c.index for c in chapters)
    for title in titles:
        matches = [c.index for c in chapters if title.casefold() in c.title.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Title pattern {title!r} matched {len(matches)} chapters; use indices")
        selected.update(matches)
    if exclude:
        selected.difference_update(parse_indices(exclude, len(chapters)))
    if skip_front_matter:
        selected.difference_update(c.index for c in chapters if c.front_matter)
    if not selected:
        raise ValueError("No chapters selected (check front-matter filtering)")
    return sorted(selected)
