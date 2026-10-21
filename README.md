# UCU Moodle Agent

Local macOS automation for the UCU Moodle course **Теорія ймовірностей та математична статистика (1.26-27.)**.

The agent discovers numbered interactive lessons and quizzes directly from the live course page; it is not limited to L5/L6. It uses your Moodle credentials stored securely in macOS Keychain and your existing Pi/Codex login (`openai-codex/gpt-5.6-sol`, medium reasoning). The password is never written to a project file or printed in logs.

## Current result

- L5: all Moodle completion requirements complete
- L6: all Moodle completion requirements complete
- Quiz 5: 100%
- Quiz 6: 100% (best attempt)

## Location

```text
/Users/mandzyuk/Documents/Documents/ДЗ UCU/ucu-moodle-agent
```

## Commands

Open Terminal and enter:

```bash
cd "/Users/mandzyuk/Documents/Documents/ДЗ UCU/ucu-moodle-agent"
```

### One-time automatic-login setup

```bash
./run-agent credentials
```

Enter your UCU Moodle username and password when prompted. The password input is hidden and both values are stored in macOS Keychain, not in the project. After this one-time setup, `lesson`, `quiz`, and `check` automatically sign in whenever the saved Moodle session expires.

Manual browser login remains available as a fallback:

```bash
./run-agent login
```

### Verify login and list courses

```bash
./run-agent check
```

### Inspect without submitting anything

```bash
./run-agent lesson --number all --dry-run
./run-agent quiz --number all --dry-run
```

### Complete a lecture and its quiz automatically

Run one discovered lesson by number:

```bash
./run-agent lesson --number 7
```

Or discover and process every currently available, unfinished lesson:

```bash
./run-agent lesson --number all
```

This is the normal command to use. `all` first scans the live course page instead of using a fixed list. It skips completed or Moodle-locked activities. Available lessons run in parallel. After the selected lessons are complete, it scans the course again to find newly unlocked quizzes and completes every available unfinished quiz in parallel. Quizzes that have closed and cannot be started are reported as skipped rather than stopping the remaining work.

By default the agent starts one worker per selected activity. To cap concurrency, set `MOODLE_PARALLEL_WORKERS`, for example:

```bash
MOODLE_PARALLEL_WORKERS=2 ./run-agent lesson --number all
```

For a new lesson attempt, the program remains in the Moodle Lesson activity for at least 10 minutes, navigates its pages, and answers the embedded tasks.

### Run only an unlocked quiz

```bash
./run-agent quiz --number 7
```

Or every discovered, available, unfinished quiz:

```bash
./run-agent quiz --number all
```

When more than one quiz is selected, quiz attempts run in parallel unless capped with `MOODLE_PARALLEL_WORKERS`.

Completed quizzes are skipped safely. Only use `--retake` when you intentionally want another attempt:

```bash
./run-agent quiz --number 6 --retake
```

### Show Chrome while running

Add `--show-browser`:

```bash
./run-agent lesson --number 5 --show-browser
```

## Reports

- Audit logs: `logs/lesson-N.jsonl` and `logs/quiz-N.jsonl`
- Question screenshots: `artifacts/quiz-N/`
- Captured interactive lesson material: `course_materials/probability/lN-visited-pages.json`
- Completion and error alerts: macOS Notification Center

When nothing needs to be repeated, completed activities are detected and skipped.

## Reinstall dependencies

The local environment is already installed. If it ever needs rebuilding:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```
