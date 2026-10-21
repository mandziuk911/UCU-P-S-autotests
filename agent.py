#!/usr/bin/env python3
"""Local entry point for the UCU Moodle assistant.

This first version lets the user log in manually, saves the authenticated
browser state in macOS Keychain, and verifies that it can access Moodle.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from moodle_runner.browser import (
    MOODLE_URL,
    automatic_login,
    is_logged_in,
    launch_browser,
    load_session,
    new_context,
    save_credentials,
    save_session,
)


def load_dependencies():
    try:
        import keyring
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(
            "Dependencies are not installed. Run:\n"
            "  python3 -m venv .venv\n"
            "  source .venv/bin/activate\n"
            "  python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return sync_playwright, keyring


def login(timeout_minutes: int) -> int:
    sync_playwright, keyring = load_dependencies()
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=False)
        saved_state = load_session(keyring)
        context = new_context(browser, saved_state)
        page = context.new_page()
        page.goto(f"{MOODLE_URL}/my/courses.php", wait_until="domcontentloaded")

        if not is_logged_in(page):
            try:
                automatic_login(page, keyring)
                page.goto(f"{MOODLE_URL}/my/courses.php", wait_until="domcontentloaded")
            except RuntimeError:
                pass

        if is_logged_in(page):
            save_session(keyring, context)
            print("Already logged in. The refreshed session is saved in macOS Keychain.")
            browser.close()
            return 0

        print("Chrome is open. Log in to UCU manually in that window.")
        print("Your password and 2FA are not read or stored by this script.")
        print(f"Waiting up to {timeout_minutes} minutes for login (Ctrl+C to cancel)...")

        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            for candidate in context.pages:
                if is_logged_in(candidate):
                    save_session(keyring, context)
                    print("Login detected. The session was saved in macOS Keychain.")
                    browser.close()
                    return 0
            time.sleep(1)

        print("Timed out before login was detected.", file=sys.stderr)
        browser.close()
        return 1


def configure_credentials() -> int:
    sync_playwright, keyring = load_dependencies()
    username = input("UCU Moodle username: ").strip()
    password = getpass.getpass("UCU Moodle password (hidden): ")
    if not username or not password:
        print("Username and password are required.", file=sys.stderr)
        return 1
    save_credentials(keyring, username, password)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=True)
        try:
            context = new_context(browser)
            page = context.new_page()
            automatic_login(page, keyring)
            save_session(keyring, context)
        except Exception as exc:
            print(f"Credentials were saved, but login failed: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()
    print("Automatic Moodle login is configured in macOS Keychain.")
    return 0


def check(show_browser: bool) -> int:
    sync_playwright, keyring = load_dependencies()
    saved_state = load_session(keyring)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=not show_browser)
        context = new_context(browser, saved_state)
        page = context.new_page()
        page.goto(f"{MOODLE_URL}/my/courses.php", wait_until="domcontentloaded")

        if not is_logged_in(page):
            automatic_login(page, keyring)
            page.goto(f"{MOODLE_URL}/my/courses.php", wait_until="domcontentloaded")
        if not is_logged_in(page):
            raise RuntimeError("Automatic Moodle login did not persist")

        course_links = page.locator('a[href*="/course/view.php?id="]')
        courses: dict[str, str] = {}
        for index in range(course_links.count()):
            link = course_links.nth(index)
            title = (link.inner_text() or "").strip()
            href = link.get_attribute("href") or ""
            if title and href:
                courses[href] = " ".join(title.split())

        print("Moodle session is valid.")
        if courses:
            print(f"Found {len(courses)} course link(s):")
            for title in courses.values():
                print(f"  - {title}")
        else:
            print("No course links were found on the My courses page.")

        save_session(keyring, context)
        browser.close()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local UCU Moodle assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "credentials", help="securely configure automatic Moodle login"
    )

    login_parser = subparsers.add_parser("login", help="Log in using a local Chrome window")
    login_parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        metavar="MINUTES",
        help="how long to wait for manual login (default: 15)",
    )

    check_parser = subparsers.add_parser("check", help="Verify login and list detected courses")
    check_parser.add_argument(
        "--show-browser",
        action="store_true",
        help="show Chrome while checking",
    )

    lesson_parser = subparsers.add_parser(
        "lesson", help="complete an interactive Moodle Lesson activity"
    )
    lesson_parser.add_argument(
        "--number",
        required=True,
        metavar="N|all",
        help="lesson number to run, or all available unfinished lessons",
    )
    lesson_parser.add_argument(
        "--show-browser",
        action="store_true",
        help="show Chrome while running",
    )
    lesson_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect the activity without clicking or submitting",
    )
    lesson_parser.add_argument(
        "--minimum-seconds",
        type=int,
        default=605,
        help=argparse.SUPPRESS,
    )

    quiz_parser = subparsers.add_parser("quiz", help="complete an unlocked Moodle quiz")
    quiz_parser.add_argument(
        "--number",
        required=True,
        metavar="N|all",
        help="quiz number to run, or all available unfinished quizzes",
    )
    quiz_parser.add_argument(
        "--show-browser",
        action="store_true",
        help="show Chrome while running",
    )
    quiz_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect availability without starting an attempt",
    )
    quiz_parser.add_argument(
        "--retake",
        action="store_true",
        help="start another attempt even when the quiz is already complete",
    )
    return parser


def _number_argument(value: str) -> int | None:
    if value.lower() == "all":
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("--number must be a positive integer or 'all'") from exc
    if number <= 0:
        raise ValueError("--number must be a positive integer or 'all'")
    return number


def _one_activity(activities: dict[int, dict], number: int, kind: str) -> dict:
    activity = activities.get(number)
    if activity is None:
        raise RuntimeError(f"{kind.title()} {number} was not found on the course page")
    if not activity["available"]:
        raise RuntimeError(f"{kind.title()} {number} is currently locked or unavailable")
    return activity


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "credentials":
            return configure_credentials()
        if args.command == "login":
            return login(args.timeout)
        if args.command == "check":
            return check(args.show_browser)
        if args.command == "lesson":
            from moodle_runner.activities import actionable, discover_activities
            from moodle_runner.lesson_runner import run_lessons
            from moodle_runner.quiz_runner import run_quizzes

            sync_playwright, keyring = load_dependencies()
            requested = _number_argument(args.number)
            with sync_playwright() as playwright:
                discovered = discover_activities(
                    playwright, keyring, show_browser=args.show_browser
                )
                if requested is None:
                    lessons = actionable(discovered["lessons"])
                else:
                    lessons = [
                        _one_activity(discovered["lessons"], requested, "lesson")
                    ]
                lesson_results = run_lessons(
                    playwright,
                    keyring,
                    lessons,
                    show_browser=args.show_browser,
                    dry_run=args.dry_run,
                    minimum_seconds=args.minimum_seconds,
                ) if lessons else []

                # Re-scan because finishing a lesson unlocks its matching quiz.
                discovered = discover_activities(
                    playwright, keyring, show_browser=args.show_browser
                )
                if requested is None:
                    quizzes = actionable(discovered["quizzes"])
                else:
                    quiz = discovered["quizzes"].get(requested)
                    quizzes = [quiz] if quiz and quiz["available"] else []
                quiz_results = run_quizzes(
                    playwright,
                    keyring,
                    quizzes,
                    show_browser=args.show_browser,
                    dry_run=args.dry_run,
                ) if quizzes else []
            for activity, result in zip(lessons, lesson_results):
                print(f"L{activity['number']}: {result}")
            for activity, result in zip(quizzes, quiz_results):
                print(f"Quiz {activity['number']}: {result}")
            return 0
        if args.command == "quiz":
            from moodle_runner.activities import actionable, discover_activities
            from moodle_runner.quiz_runner import run_quizzes

            sync_playwright, keyring = load_dependencies()
            requested = _number_argument(args.number)
            with sync_playwright() as playwright:
                discovered = discover_activities(
                    playwright, keyring, show_browser=args.show_browser
                )
                if requested is None:
                    if args.retake:
                        quizzes = [
                            discovered["quizzes"][number]
                            for number in sorted(discovered["quizzes"])
                            if discovered["quizzes"][number]["available"]
                        ]
                    else:
                        quizzes = actionable(discovered["quizzes"])
                else:
                    quizzes = [
                        _one_activity(discovered["quizzes"], requested, "quiz")
                    ]
                results = run_quizzes(
                    playwright,
                    keyring,
                    quizzes,
                    show_browser=args.show_browser,
                    dry_run=args.dry_run,
                    retake=args.retake,
                ) if quizzes else []
            for activity, result in zip(quizzes, results):
                print(f"Quiz {activity['number']}: {result}")
            return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
