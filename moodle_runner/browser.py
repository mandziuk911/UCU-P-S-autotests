"""Browser and Keychain session helpers."""

from __future__ import annotations

import json
import threading

_SESSION_LOCK = threading.Lock()

MOODLE_URL = "https://learn.ucu.edu.ua"
KEYCHAIN_SERVICE = "ucu-moodle-agent"
KEYCHAIN_ACCOUNT = "moodle-session"
KEYCHAIN_USERNAME_ACCOUNT = "moodle-username"
KEYCHAIN_PASSWORD_ACCOUNT = "moodle-password"


def load_session(keyring):
    raw_state = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if not raw_state:
        return None
    try:
        return json.loads(raw_state)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The session stored in macOS Keychain is invalid.") from exc


def save_session(keyring, context) -> None:
    state = json.dumps(context.storage_state(), separators=(",", ":"))
    with _SESSION_LOCK:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, state)


def save_credentials(keyring, username: str, password: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME_ACCOUNT, username)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_PASSWORD_ACCOUNT, password)


def load_credentials(keyring) -> tuple[str, str] | None:
    username = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME_ACCOUNT)
    password = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_PASSWORD_ACCOUNT)
    if not username or not password:
        return None
    return username, password


def new_context(browser, saved_state=None):
    options = {"no_viewport": True}
    if saved_state:
        options["storage_state"] = saved_state
    return browser.new_context(**options)


def automatic_login(page, keyring) -> None:
    credentials = load_credentials(keyring)
    if credentials is None:
        raise RuntimeError(
            "Moodle credentials are not configured. Run: ./run-agent credentials"
        )
    username, password = credentials
    page.goto(f"{MOODLE_URL}/login/index.php", wait_until="domcontentloaded")
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.locator("#loginbtn").click()
    page.wait_for_load_state("domcontentloaded")
    if not is_logged_in(page):
        error = page.locator(".loginerrors, .alert-danger").first
        detail = " ".join(error.inner_text().split()) if error.count() else "login rejected"
        raise RuntimeError(
            f"Automatic Moodle login failed ({detail}). "
            "Update it with: ./run-agent credentials"
        )
    save_session(keyring, page.context)


def launch_browser(playwright, *, headless: bool):
    try:
        return playwright.chromium.launch(channel="chrome", headless=headless)
    except Exception as exc:
        raise RuntimeError(
            "Could not launch Google Chrome. Make sure Chrome is installed."
        ) from exc


def is_logged_in(page) -> bool:
    try:
        return page.locator('a[href*="/login/logout.php"]').count() > 0
    except Exception:
        return False
