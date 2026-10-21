"""macOS Notification Center integration."""

from __future__ import annotations

import subprocess


_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
""".strip()


def notify(message: str, title: str = "UCU Moodle Agent") -> None:
    try:
        subprocess.run(
            ["osascript", "-e", _SCRIPT, title[:80], message[:240]],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
