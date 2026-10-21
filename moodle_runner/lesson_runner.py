"""Stateful runner for dynamically discovered Moodle Lesson activities."""

from __future__ import annotations

import fcntl
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .activities import COURSE_ID
from .audit import AuditLog
from .browser import (
    MOODLE_URL,
    automatic_login,
    is_logged_in,
    launch_browser,
    load_session,
    new_context,
    save_session,
)
from .materials import capture_lesson_page
from .notifications import notify
from .pi_solver import solve_question
from .questions import fill_answer, find_question_form, parse_question

_NEXT_LABEL = re.compile(
    r"^(start|next|next item|continue|finish|end lesson|далі|продовжити|почати)$",
    re.IGNORECASE,
)
_END_TEXT = (
    "end of lesson",
    "кінець лекції",
    "досягли кінця",
    "completed this lesson",
)
_INCORRECT_TEXT = (
    "incorrect",
    "not correct",
    "неправиль",
    "невірн",
)


class LessonRunner:
    def __init__(
        self,
        playwright,
        keyring,
        activity: dict,
        *,
        show_browser: bool = False,
        dry_run: bool = False,
        minimum_seconds: int = 605,
    ) -> None:
        if activity.get("kind") != "lesson":
            raise ValueError("LessonRunner requires a discovered lesson activity")
        self.playwright = playwright
        self.keyring = keyring
        self.lesson_number = int(activity["number"])
        self.config = activity
        self.show_browser = show_browser
        self.dry_run = dry_run
        self.minimum_seconds = minimum_seconds
        self.audit = AuditLog(self.lesson_number)
        self.attempts: dict[str, int] = {}
        self.feedback: dict[str, str] = {}
        self.started_at: float | None = None
        self.minimum_already_satisfied = False
        self.lesson_already_complete = False

    def _validate_url(self, page) -> None:
        parsed = urlparse(page.url)
        if parsed.scheme != "https" or parsed.netloc != "learn.ucu.edu.ua":
            raise RuntimeError(f"Refusing unexpected Moodle redirect: {page.url}")

    @staticmethod
    def _main_text(page) -> str:
        main = page.locator("main, #region-main").first
        return "\n".join(line.strip() for line in main.inner_text().splitlines() if line.strip())

    def _enter_lesson(self, page) -> None:
        activity_id = self.config["activity_id"]
        page.goto(
            f"{MOODLE_URL}/mod/lesson/view.php?id={activity_id}",
            wait_until="domcontentloaded",
        )
        self._validate_url(page)
        if not is_logged_in(page):
            automatic_login(page, self.keyring)
            page.goto(
                f"{MOODLE_URL}/mod/lesson/view.php?id={activity_id}",
                wait_until="domcontentloaded",
            )
            self._validate_url(page)
            if not is_logged_in(page):
                raise RuntimeError("Automatic Moodle login did not persist")

        initial_text = self._main_text(page)
        self.minimum_already_satisfied = bool(
            re.search(r"Зроблено:\s*Витратьте принаймні", initial_text, re.I)
            or re.search(r"Done:\s*Spend at least", initial_text, re.I)
        )
        self.lesson_already_complete = (
            initial_text.count("Зроблено:") >= 3
            or initial_text.lower().count("done:") >= 3
        )

        if self.dry_run:
            print(f"Dry run: opened L{self.lesson_number}: {self.config['name']}")
            print(initial_text[:2500])
            return

        if self.lesson_already_complete:
            print(f"L{self.lesson_number}: Moodle already marks all lesson requirements complete")
            self.audit.record("lesson_already_complete", activity_id=activity_id)
            return

        resume = page.locator('a[href*="startlastseen=yes"]')
        if resume.count():
            print(f"L{self.lesson_number}: resuming the existing lesson attempt")
            resume.first.click()
            page.wait_for_load_state("domcontentloaded")
        else:
            form = self._navigation_form(page, labels={"start", "почати"})
            if form is not None:
                print(f"L{self.lesson_number}: starting the lesson")
                self._click_form(form, page)
            else:
                print(f"L{self.lesson_number}: continuing from the current page")

        self.started_at = time.monotonic()
        self.audit.record("lesson_entered", activity_id=activity_id, url=page.url)

    def _wait_minimum_time(self, page) -> None:
        if self.minimum_already_satisfied:
            print(f"L{self.lesson_number}: Moodle already marks the 10-minute rule complete")
            self.audit.record("minimum_time_already_satisfied")
            return
        if self.dry_run or self.minimum_seconds <= 0 or self.started_at is None:
            return
        target = self.started_at + self.minimum_seconds
        while True:
            remaining = target - time.monotonic()
            if remaining <= 0:
                break
            minutes = max(1, int((remaining + 59) // 60))
            print(
                f"L{self.lesson_number}: keeping the lesson active; "
                f"about {minutes} minute(s) remaining"
            )
            page.wait_for_timeout(min(60, remaining) * 1000)
        self.audit.record("minimum_time_satisfied", seconds=self.minimum_seconds)

    @staticmethod
    def _click_form(form, page) -> None:
        button = form.locator('button, input[type="submit"]').first
        if not button.count():
            raise RuntimeError("Moodle navigation form has no submit control")
        button.click()
        page.wait_for_load_state("domcontentloaded")

    def _navigation_form(self, page, labels: set[str] | None = None):
        forms = page.locator('form[action*="/mod/lesson/continue.php"]')
        candidates = []
        for index in range(forms.count()):
            form = forms.nth(index)
            control = form.locator('button, input[type="submit"]').first
            if not control.count():
                continue
            label = " ".join(
                (
                    control.inner_text()
                    or control.get_attribute("value")
                    or control.get_attribute("aria-label")
                    or ""
                ).split()
            )
            lower = label.lower()
            if labels and lower in labels:
                return form
            if _NEXT_LABEL.match(label):
                candidates.append(form)
        return candidates[-1] if candidates else None

    def _handle_feedback(self, page, question_id: str) -> None:
        text = self._main_text(page)
        lower = text.lower()
        excerpt = text[:2000]
        self.feedback[question_id] = excerpt
        incorrect = any(marker in lower for marker in _INCORRECT_TEXT)
        self.audit.record(
            "answer_feedback",
            question_id=question_id,
            incorrect=incorrect,
            feedback=excerpt[:800],
        )

        continue_form = self._navigation_form(page)
        if continue_form is not None:
            self._click_form(continue_form, page)
            return

        links = page.get_by_role(
            "link", name=re.compile(r"^(continue|next|продовжити|далі)$", re.I)
        )
        if links.count():
            links.first.click()
            page.wait_for_load_state("domcontentloaded")
            return

        # This Moodle theme sometimes renders answer feedback without a Continue
        # control. Moodle still records the next last-seen page, so return to the
        # activity entry and use its normal Resume link.
        activity_id = self.config["activity_id"]
        page.goto(
            f"{MOODLE_URL}/mod/lesson/view.php?id={activity_id}",
            wait_until="domcontentloaded",
        )
        resume = page.locator('a[href*="startlastseen=yes"]')
        if not resume.count():
            raise RuntimeError("Feedback page had no Continue control or Resume link")
        resume.first.click()
        page.wait_for_load_state("domcontentloaded")

    def _answer_question(self, page, form) -> None:
        question = parse_question(form)
        question_id = question["question_id"]
        attempt = self.attempts.get(question_id, 0) + 1
        if attempt > 3:
            raise RuntimeError(
                f"Question {question['page_id']} was still unresolved after 3 attempts"
            )
        self.attempts[question_id] = attempt
        print(
            f"L{self.lesson_number}: solving {question['type']} question "
            f"on page {question['page_id']} (attempt {attempt})"
        )
        self.audit.record(
            "question_detected",
            question_id=question_id,
            page_id=question["page_id"],
            question_type=question["type"],
            stem=question["stem"][:1000],
            attempt=attempt,
        )
        solution = solve_question(
            self.lesson_number,
            question,
            previous_feedback=self.feedback.get(question_id),
        )
        safe_solution = {
            key: value
            for key, value in solution.items()
            if key in {"answer", "choice_text", "choice_value", "choice_texts", "choice_names", "matches", "confidence", "reason"}
        }
        self.audit.record(
            "answer_generated",
            question_id=question_id,
            attempt=attempt,
            solution=safe_solution,
        )
        fill_answer(form, question, solution)
        submit = form.locator('[name="submitbutton"]').first
        if not submit.count():
            raise RuntimeError("Question form has no submit button")
        submit.click()
        page.wait_for_load_state("domcontentloaded")
        self.audit.record("answer_submitted", question_id=question_id, attempt=attempt)
        self._handle_feedback(page, question_id)

    def _is_end_page(self, page) -> bool:
        if "pageid=-9" in page.url:
            return True
        lower = self._main_text(page).lower()
        return any(marker in lower for marker in _END_TEXT)

    def _navigate_content(self, page) -> None:
        form = self._navigation_form(page)
        if form is None:
            labels = page.locator(
                'form[action*="/mod/lesson/continue.php"] button, '
                'form[action*="/mod/lesson/continue.php"] input[type="submit"]'
            ).evaluate_all(
                "controls => controls.map(c => (c.innerText || c.value || '').trim())"
            )
            raise RuntimeError(
                f"No safe Next/Continue control found on {page.url}; controls={labels}"
            )
        self._click_form(form, page)

    def _verify(self, page) -> dict:
        activity_id = self.config["activity_id"]
        page.goto(f"{MOODLE_URL}/course/view.php?id={COURSE_ID}", wait_until="domcontentloaded")
        lesson_link = page.locator(
            f'a[href*="/mod/lesson/view.php?id={activity_id}"]'
        ).first
        if not lesson_link.count():
            raise RuntimeError("Could not find the lesson on the expected course page")
        card = lesson_link.locator("xpath=ancestor::*[self::li or self::div][contains(@class,'activity')][1]")
        lesson_status = " ".join((card.inner_text() if card.count() else lesson_link.inner_text()).split())

        quiz_text = f"Quiz {self.lesson_number}"
        quiz = page.get_by_text(quiz_text, exact=True).first
        quiz_status = "not found"
        unlocked = False
        if quiz.count():
            quiz_card = quiz.locator(
                "xpath=ancestor::*[self::li or self::div][contains(@class,'activity')][1]"
            )
            quiz_status = " ".join(
                (quiz_card.inner_text() if quiz_card.count() else quiz.inner_text()).split()
            )
            quiz_link = quiz.locator("xpath=ancestor-or-self::a[1]")
            unlocked = quiz_link.count() > 0 and bool(quiz_link.get_attribute("href"))

        result = {
            "lesson_status": lesson_status,
            "quiz_status": quiz_status,
            "quiz_unlocked": unlocked,
        }
        self.audit.record("completion_verified", **result)
        return result

    def run(self) -> dict:
        saved_state = load_session(self.keyring)
        browser = launch_browser(self.playwright, headless=not self.show_browser)
        try:
            context = new_context(browser, saved_state)
            page = context.new_page()
            self._enter_lesson(page)
            if self.dry_run:
                save_session(self.keyring, context)
                return {"dry_run": True, "url": page.url}

            if self.lesson_already_complete:
                result = self._verify(page)
                save_session(self.keyring, context)
                if not result["quiz_unlocked"]:
                    raise RuntimeError(
                        f"L{self.lesson_number} is complete, but Quiz {self.lesson_number} "
                        f"is still locked. Status: {result['quiz_status']}"
                    )
                return result

            # Moodle measures the active lesson attempt. Waiting after Start/Resume
            # guarantees the minimum-time rule before any final completion action.
            self._wait_minimum_time(page)

            for step in range(1, 101):
                self._validate_url(page)
                if not is_logged_in(page):
                    raise RuntimeError("Moodle login expired during the lesson")
                capture_lesson_page(self.lesson_number, page.url, self._main_text(page))
                if self._is_end_page(page):
                    self.audit.record("lesson_end_reached", step=step, url=page.url)
                    break
                form = find_question_form(page)
                if form is not None:
                    self._answer_question(page, form)
                else:
                    self.audit.record("content_page", step=step, url=page.url)
                    self._navigate_content(page)
            else:
                raise RuntimeError("Lesson exceeded the 100-page safety limit")

            result = self._verify(page)
            save_session(self.keyring, context)
            if not result["quiz_unlocked"]:
                raise RuntimeError(
                    f"L{self.lesson_number} ended, but Quiz {self.lesson_number} is still locked. "
                    f"Lesson status: {result['lesson_status']}"
                )
            notify(
                f"L{self.lesson_number} completed; Quiz {self.lesson_number} is unlocked."
            )
            return result
        except Exception as exc:
            self.audit.record("run_failed", error=str(exc))
            notify(f"L{self.lesson_number} needs attention: {exc}")
            raise
        finally:
            browser.close()


def _parallel_workers(activity_count: int) -> int:
    configured = os.environ.get("MOODLE_PARALLEL_WORKERS")
    if configured:
        try:
            return max(1, min(activity_count, int(configured)))
        except ValueError as exc:
            raise RuntimeError("MOODLE_PARALLEL_WORKERS must be a positive integer") from exc
    return activity_count


def _run_lesson_worker(
    keyring,
    activity: dict,
    *,
    show_browser: bool,
    dry_run: bool,
    minimum_seconds: int,
) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as worker_playwright:
        return LessonRunner(
            worker_playwright,
            keyring,
            activity,
            show_browser=show_browser,
            dry_run=dry_run,
            minimum_seconds=minimum_seconds,
        ).run()


def run_lessons(
    playwright,
    keyring,
    lesson_activities: list[dict],
    *,
    show_browser: bool,
    dry_run: bool,
    minimum_seconds: int,
) -> list[dict]:
    lock_path = Path.home() / ".ucu-moodle-agent" / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another UCU Moodle agent run is already active") from exc

        if len(lesson_activities) <= 1:
            return [
                LessonRunner(
                    playwright,
                    keyring,
                    activity,
                    show_browser=show_browser,
                    dry_run=dry_run,
                    minimum_seconds=minimum_seconds,
                ).run()
                for activity in lesson_activities
            ]

        workers = _parallel_workers(len(lesson_activities))
        print(f"Running {len(lesson_activities)} lesson(s) with {workers} parallel worker(s)")
        results: list[dict | None] = [None] * len(lesson_activities)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_lesson_worker,
                    keyring,
                    activity,
                    show_browser=show_browser,
                    dry_run=dry_run,
                    minimum_seconds=minimum_seconds,
                ): index
                for index, activity in enumerate(lesson_activities)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [result for result in results if result is not None]
