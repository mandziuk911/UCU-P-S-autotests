"""Discover numbered Lesson and Quiz activities from the live Moodle course page."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .browser import (
    MOODLE_URL,
    automatic_login,
    is_logged_in,
    launch_browser,
    load_session,
    new_context,
    save_session,
)

COURSE_ID = 9400
_LESSON_NUMBER = re.compile(r"\bL\s*(\d+)\b", re.I)
_QUIZ_NUMBER = re.compile(r"\bQuiz\s*(\d+)\b", re.I)


def _activity_id(card, href: str | None) -> int:
    card_id = card.get_attribute("id") or ""
    match = re.fullmatch(r"module-(\d+)", card_id)
    if match:
        return int(match.group(1))
    if href:
        values = parse_qs(urlparse(href).query).get("id", [])
        if values and values[0].isdigit():
            return int(values[0])
    raise RuntimeError(f"Could not determine Moodle activity id for {card_id or 'activity'}")


def _is_complete(text: str) -> bool:
    lower = text.lower()
    return bool(
        re.search(r"(?:^|\s)готово(?:\s|$)", lower)
        or re.search(r"(?:^|\s)done(?:\s|$)", lower)
    )


def _parse_cards(page, kind: str) -> dict[int, dict]:
    if kind == "lesson":
        selector = ".activity.modtype_lesson"
        module_path = "/mod/lesson/view.php"
        number_pattern = _LESSON_NUMBER
    elif kind == "quiz":
        selector = ".activity.modtype_quiz"
        module_path = "/mod/quiz/view.php"
        number_pattern = _QUIZ_NUMBER
    else:
        raise ValueError(f"Unsupported activity kind: {kind}")

    found: dict[int, dict] = {}
    cards = page.locator(selector)
    for index in range(cards.count()):
        card = cards.nth(index)
        text = " ".join(card.inner_text().split())
        name_node = card.locator(".activityname").first
        name = " ".join(name_node.inner_text().split()) if name_node.count() else text
        match = number_pattern.search(name)
        if not match:
            continue
        number = int(match.group(1))
        links = card.locator(f'a[href*="{module_path}"]')
        href = links.first.get_attribute("href") if links.count() else None
        activity_id = _activity_id(card, href)
        found[number] = {
            "kind": kind,
            "number": number,
            "activity_id": activity_id,
            "name": name,
            "href": href,
            "available": bool(href),
            "completed": _is_complete(text),
            "status": text,
        }
    return found


def discover_activities(playwright, keyring, *, show_browser: bool = False) -> dict[str, dict[int, dict]]:
    """Authenticate, scan the course page, and return numbered lessons/quizzes."""
    browser = launch_browser(playwright, headless=not show_browser)
    try:
        context = new_context(browser, load_session(keyring))
        page = context.new_page()
        course_url = f"{MOODLE_URL}/course/view.php?id={COURSE_ID}"
        page.goto(course_url, wait_until="domcontentloaded")
        if not is_logged_in(page):
            automatic_login(page, keyring)
            page.goto(course_url, wait_until="domcontentloaded")
        if not is_logged_in(page):
            raise RuntimeError("Automatic Moodle login did not persist")
        result = {
            "lessons": _parse_cards(page, "lesson"),
            "quizzes": _parse_cards(page, "quiz"),
        }
        save_session(keyring, context)
        return result
    finally:
        browser.close()


def actionable(activities: dict[int, dict]) -> list[dict]:
    """Return available, unfinished activities in numeric order."""
    return [
        activities[number]
        for number in sorted(activities)
        if activities[number]["available"] and not activities[number]["completed"]
    ]
