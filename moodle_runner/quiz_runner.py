"""Runner for unlocked, timed Moodle quizzes."""

from __future__ import annotations

import fcntl
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

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
from .notifications import notify
from .pi_solver import solve_question
from .quiz_questions import fill_quiz_answer, parse_quiz_question

_SUBMIT_ALL = re.compile(r"(submit all and finish|відправити все та завершити)", re.I)
_CHECK = re.compile(r"^(check|перевірити)$", re.I)
_TRY_AGAIN = re.compile(r"(try again|спробувати ще раз)", re.I)


class QuizRunner:
    def __init__(
        self,
        playwright,
        keyring,
        activity: dict,
        *,
        show_browser: bool,
        dry_run: bool,
        retake: bool = False,
    ):
        if activity.get("kind") != "quiz":
            raise ValueError("QuizRunner requires a discovered quiz activity")
        self.playwright = playwright
        self.keyring = keyring
        self.quiz_number = int(activity["number"])
        self.activity_id = int(activity["activity_id"])
        self.show_browser = show_browser
        self.dry_run = dry_run
        self.retake = retake
        self.audit = AuditLog(self.quiz_number, kind="quiz")
        self.deadline: float | None = None

    @staticmethod
    def _main_text(page) -> str:
        main = page.locator("main, #region-main").first
        return "\n".join(line.strip() for line in main.inner_text().splitlines() if line.strip())

    @staticmethod
    def _validate_url(page) -> None:
        parsed = urlparse(page.url)
        if parsed.scheme != "https" or parsed.netloc != "learn.ucu.edu.ua":
            raise RuntimeError(f"Refusing unexpected redirect: {page.url}")

    @staticmethod
    def _visible_control(locator):
        for index in range(locator.count()):
            control = locator.nth(index)
            if control.is_visible():
                return control
        return None

    def _ensure_time(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise RuntimeError("Stopped before Moodle's quiz timer expired")

    def _open_or_start(self, page) -> str:
        page.goto(
            f"{MOODLE_URL}/mod/quiz/view.php?id={self.activity_id}",
            wait_until="domcontentloaded",
        )
        self._validate_url(page)
        if not is_logged_in(page):
            automatic_login(page, self.keyring)
            page.goto(
                f"{MOODLE_URL}/mod/quiz/view.php?id={self.activity_id}",
                wait_until="domcontentloaded",
            )
            self._validate_url(page)
            if not is_logged_in(page):
                raise RuntimeError("Automatic Moodle login did not persist")
        text = self._main_text(page)

        if self.dry_run:
            print(f"Dry run: Quiz {self.quiz_number}\n{text[:2200]}")
            return "dry_run"

        if not self.retake and (
            re.search(r"Зроблено:\s*Отримати оцінку", text, re.I)
            or ("Done:" in text and "Grade" in text)
        ):
            print(f"Quiz {self.quiz_number}: already completed")
            return "completed"

        resume = page.locator('a[href*="/mod/quiz/attempt.php?attempt="]')
        if resume.count():
            print(f"Quiz {self.quiz_number}: resuming active attempt")
            resume.first.click()
            page.wait_for_load_state("domcontentloaded")
            self.deadline = time.monotonic() + 600
            return "attempt"

        if re.search(r"(Продовжуйте свою спробу|Continue your attempt)", text, re.I):
            print(f"Quiz {self.quiz_number}: resuming active attempt")
            resume_form = page.locator(
                'form[action*="/mod/quiz/startattempt.php"]:not(#mod_quiz_preflight_form)'
            ).first
            resume_form.locator('button[type="submit"], input[type="submit"]').first.click()
            page.wait_for_load_state("domcontentloaded")
            if "/mod/quiz/attempt.php" not in page.url:
                raise RuntimeError(f"Could not resume quiz attempt: {page.url}")
            self.deadline = time.monotonic() + 480
            return "attempt"

        start_form = page.locator(
            'form[action*="/mod/quiz/startattempt.php"]:not(#mod_quiz_preflight_form)'
        ).first
        if not start_form.count():
            print(f"Quiz {self.quiz_number}: not currently available to start; skipped")
            self.audit.record("quiz_unavailable", quiz=self.quiz_number, url=page.url)
            return "unavailable"
        print(f"Quiz {self.quiz_number}: starting 12-minute attempt")
        start_form.locator('button[type="submit"], input[type="submit"]').first.click()
        preflight = page.locator("#mod_quiz_preflight_form")
        preflight.wait_for(state="visible", timeout=10000)
        preflight.locator('input[name="submitbutton"]').click()
        page.wait_for_load_state("domcontentloaded")
        if "/mod/quiz/attempt.php" not in page.url:
            raise RuntimeError(f"Moodle did not open a quiz attempt: {page.url}")
        self.deadline = time.monotonic() + 660
        self.audit.record("quiz_started", quiz=self.quiz_number, url=page.url)
        return "attempt"

    def _question_feedback(self, container) -> str:
        feedback = container.locator(".outcome, .feedback, .specificfeedback, .generalfeedback")
        parts = []
        for index in range(feedback.count()):
            text = " ".join(feedback.nth(index).inner_text().split())
            if text:
                parts.append(text)
        return "\n".join(parts)[:2500]

    def _solve_container(self, page, container_id: str) -> None:
        attempts = 0
        previous_feedback = None
        while attempts < 2:
            self._ensure_time()
            container = page.locator(f"#{container_id}")
            if not container.count():
                raise RuntimeError(f"Question disappeared from the attempt: {container_id}")
            classes = set((container.get_attribute("class") or "").split())
            if "correct" in classes:
                return

            attempts += 1
            question = parse_quiz_question(container)
            print(
                f"Quiz {self.quiz_number}: solving question "
                f"{question['container_id']} ({question['type']}, attempt {attempts})"
            )
            artifact_dir = Path("artifacts") / f"quiz-{self.quiz_number}"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            image_path = artifact_dir / f"{question['question_id']}.png"
            container.screenshot(path=str(image_path))
            self.audit.record(
                "quiz_question_detected",
                question_id=question["question_id"],
                container_id=container_id,
                question_type=question["type"],
                stem=question["stem"][:1200],
                attempt=attempts,
            )
            solution = solve_question(
                self.quiz_number,
                question,
                previous_feedback=previous_feedback,
                image_path=image_path,
            )
            fill_quiz_answer(container, question, solution)
            self.audit.record(
                "quiz_answer_generated",
                question_id=question["question_id"],
                attempt=attempts,
                confidence=solution.get("confidence"),
                reason=solution.get("reason", "")[:1000],
            )

            check = self._visible_control(
                container.get_by_role("button", name=_CHECK)
            )
            if check is None:
                # Deferred-feedback quizzes save all filled controls on Next.
                return
            check.click()
            page.wait_for_load_state("domcontentloaded")
            container = page.locator(f"#{container_id}")
            classes = set((container.get_attribute("class") or "").split())
            feedback = self._question_feedback(container)
            self.audit.record(
                "quiz_answer_checked",
                question_id=question["question_id"],
                correct="correct" in classes,
                feedback=feedback,
            )
            if "correct" in classes:
                return
            previous_feedback = feedback or "Moodle marked the previous answer incorrect."
            retry = self._visible_control(container.get_by_role("button", name=_TRY_AGAIN))
            if retry is None:
                raise RuntimeError(
                    f"Question {container_id} is incorrect and Moodle offers no retry"
                )
            retry.click()
            page.wait_for_load_state("domcontentloaded")
        raise RuntimeError(f"Question {container_id} remains incorrect after two attempts")

    def _complete_attempt(self, page) -> None:
        for page_number in range(1, 11):
            self._ensure_time()
            containers = page.locator(".que")
            if not containers.count():
                break
            ids = [containers.nth(i).get_attribute("id") for i in range(containers.count())]
            for container_id in ids:
                if container_id:
                    self._solve_container(page, container_id)

            next_button = self._visible_control(
                page.locator(
                    '#responseform input[name="next"], #responseform button[name="next"], '
                    '#mod_quiz-next-nav'
                )
            )
            if next_button is not None:
                next_button.click()
                page.wait_for_load_state("domcontentloaded")
                if "/mod/quiz/summary.php" in page.url:
                    break
                continue

            finish = self._visible_control(
                page.locator("a.endtestlink, a[href*='/mod/quiz/summary.php']")
            )
            if finish is None:
                raise RuntimeError("Could not find Next or Finish attempt control")
            finish.click()
            page.wait_for_load_state("domcontentloaded")
            break
        else:
            raise RuntimeError("Quiz exceeded the 10-page safety limit")

        if "/mod/quiz/summary.php" not in page.url:
            raise RuntimeError(f"Expected quiz summary page, got {page.url}")
        summary = self._main_text(page)
        if re.search(r"(not yet answered|ще не дано відповіді)", summary, re.I):
            raise RuntimeError("Quiz summary reports unanswered questions")

        submit = self._visible_control(page.get_by_role("button", name=_SUBMIT_ALL))
        if submit is None:
            submit = self._visible_control(
                page.locator('button:has-text("Відправити все"), input[value*="Відправити все"]')
            )
        if submit is None:
            raise RuntimeError("Could not find Submit all and finish")
        submit.click()
        page.wait_for_timeout(700)

        # Some Moodle themes submit immediately; others open a confirmation modal.
        if "/mod/quiz/summary.php" in page.url:
            modal_submit = page.locator(".modal.show").get_by_role(
                "button", name=_SUBMIT_ALL
            )
            modal_submit.wait_for(state="visible", timeout=10000)
            modal_submit.click()
            page.wait_for_load_state("domcontentloaded")
        self.audit.record("quiz_submitted", quiz=self.quiz_number, url=page.url)

    def _result(self, page) -> dict:
        text = self._main_text(page)
        grade_match = re.search(
            r"(?:Оцінка|Grade)\s*[:\t ]+([^\n]+)", text, re.I
        )
        result = {
            "quiz": self.quiz_number,
            "url": page.url,
            "grade": grade_match.group(1).strip() if grade_match else "See Moodle",
        }
        self.audit.record("quiz_result", **result)
        return result

    def run(self) -> dict:
        state = load_session(self.keyring)
        browser = launch_browser(self.playwright, headless=not self.show_browser)
        try:
            context = new_context(browser, state)
            page = context.new_page()
            status = self._open_or_start(page)
            if status == "dry_run":
                save_session(self.keyring, context)
                return {"quiz": self.quiz_number, "dry_run": True}
            if status == "unavailable":
                save_session(self.keyring, context)
                return {"quiz": self.quiz_number, "skipped": "not available to start"}
            if status == "attempt":
                self._complete_attempt(page)
            result = self._result(page)
            save_session(self.keyring, context)
            notify(f"Quiz {self.quiz_number} submitted. Result: {result['grade']}")
            return result
        except Exception as exc:
            self.audit.record("quiz_failed", error=str(exc))
            notify(f"Quiz {self.quiz_number} needs attention: {exc}")
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


def _run_quiz_worker(
    keyring,
    activity: dict,
    *,
    show_browser: bool,
    dry_run: bool,
    retake: bool,
) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as worker_playwright:
        return QuizRunner(
            worker_playwright,
            keyring,
            activity,
            show_browser=show_browser,
            dry_run=dry_run,
            retake=retake,
        ).run()


def run_quizzes(
    playwright,
    keyring,
    quiz_activities: list[dict],
    *,
    show_browser: bool,
    dry_run: bool,
    retake: bool = False,
):
    lock_path = Path.home() / ".ucu-moodle-agent" / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another UCU Moodle agent run is already active") from exc

        if len(quiz_activities) <= 1:
            return [
                QuizRunner(
                    playwright,
                    keyring,
                    activity,
                    show_browser=show_browser,
                    dry_run=dry_run,
                    retake=retake,
                ).run()
                for activity in quiz_activities
            ]

        workers = _parallel_workers(len(quiz_activities))
        print(f"Running {len(quiz_activities)} quiz(zes) with {workers} parallel worker(s)")
        results: list[dict | None] = [None] * len(quiz_activities)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_quiz_worker,
                    keyring,
                    activity,
                    show_browser=show_browser,
                    dry_run=dry_run,
                    retake=retake,
                ): index
                for index, activity in enumerate(quiz_activities)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [result for result in results if result is not None]
