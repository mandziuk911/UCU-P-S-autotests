"""Local retrieval and capture for interactive Moodle lesson content."""

from __future__ import annotations

import json
import re
from pathlib import Path

_WORD_RE = re.compile(r"[A-Za-z0-9λμσρ]+")


def _terms(text: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(text) if len(word) > 2}


def _material_dir() -> Path:
    path = Path("course_materials") / "probability"
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture_lesson_page(lesson_number: int, url: str, text: str) -> None:
    """Persist pages visited during a live lesson for its quiz solver."""
    clean = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not clean:
        return
    path = _material_dir() / f"l{lesson_number}-visited-pages.json"
    pages = []
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                pages = value
        except json.JSONDecodeError:
            pages = []
    record = {"url": url, "text": clean}
    for index, page in enumerate(pages):
        if page.get("url") == url:
            pages[index] = record
            break
    else:
        pages.append(record)
    path.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")


def load_relevant_material(lesson_number: int, question: str, limit: int = 7) -> list[dict[str, str]]:
    """Retrieve captured or previously crawled interactive Lesson content."""
    base = _material_dir()
    paths = [
        base / f"l{lesson_number}-lesson-pages.json",
        base / f"l{lesson_number}-visited-pages.json",
    ]
    pages: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            pages.extend(page for page in value if isinstance(page, dict))

    query_terms = _terms(question)
    candidates: list[tuple[int, dict[str, str]]] = []
    for index, page in enumerate(pages):
        text = page.get("text", "")
        if not text or text.startswith("Неможливо"):
            continue
        content = text.split("Отримати оцінку", 1)[-1].strip()
        if not content:
            continue
        page_ref = page.get("requested_pageid") or page.get("url") or index + 1
        score = len(query_terms & _terms(content))
        candidates.append(
            (
                score,
                {
                    "source": f"Moodle Lesson {lesson_number}, page {page_ref}",
                    "text": content[:8000],
                },
            )
        )

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    selected = [item for score, item in candidates[:limit] if score > 0]
    if not selected:
        selected = [item for _, item in candidates[: min(limit, 3)]]
    return selected
