"""Parse and fill standard Moodle quiz question containers."""

from __future__ import annotations

import hashlib


def _control_label(control) -> str:
    text = control.evaluate(
        "element => element.labels?.[0]?.innerText "
        "|| element.closest('label')?.innerText "
        "|| element.getAttribute('aria-label') "
        "|| element.parentElement?.innerText || ''"
    )
    return " ".join((text or "").split())


def parse_quiz_question(container) -> dict:
    classes = set((container.get_attribute("class") or "").split())
    qtext = container.locator(".qtext").first
    stem = " ".join(qtext.inner_text().split()) if qtext.count() else ""
    if not stem:
        raise RuntimeError("Quiz question has no readable text")
    container_id = container.get_attribute("id") or ""
    question_id = hashlib.sha256(f"{container_id}:{stem}".encode()).hexdigest()[:16]
    question: dict = {
        "question_id": question_id,
        "container_id": container_id,
        "stem": stem,
    }

    radios = container.locator('input[type="radio"][name*="_answer"]')
    checkboxes = container.locator('input[type="checkbox"][name*="_choice"]')
    selects = container.locator("select[name]")
    text_inputs = container.locator(
        'input[type="text"][name], input[type="number"][name], textarea[name]'
    )

    if radios.count():
        question["type"] = "single_choice"
        question["choices"] = [
            {
                "name": radios.nth(i).get_attribute("name") or "",
                "value": radios.nth(i).get_attribute("value") or "",
                "text": _control_label(radios.nth(i)) or f"Option {i + 1}",
            }
            for i in range(radios.count())
        ]
    elif checkboxes.count():
        question["type"] = "multi_choice"
        question["choices"] = [
            {
                "name": checkboxes.nth(i).get_attribute("name") or "",
                "value": checkboxes.nth(i).get_attribute("value") or "",
                "text": _control_label(checkboxes.nth(i)) or f"Option {i + 1}",
            }
            for i in range(checkboxes.count())
        ]
    elif selects.count():
        question["type"] = "matching"
        rows = []
        for i in range(selects.count()):
            select = selects.nth(i)
            name = select.get_attribute("name") or ""
            row_text = select.locator(
                "xpath=ancestor::tr[1]/*[contains(concat(' ', normalize-space(@class), ' '), ' text ')][1]"
            )
            label = (
                " ".join(row_text.inner_text().split())
                if row_text.count()
                else _control_label(select)
            )
            label = label or f"Blank {i + 1} ({name})"
            options = select.locator("option").evaluate_all(
                "options => options.filter(o => o.value).map(o => "
                "({value:o.value, text:o.textContent.trim()}))"
            )
            rows.append({"name": name, "label": label, "options": options})
        question["rows"] = rows
    elif text_inputs.count():
        question["type"] = (
            "numerical"
            if classes & {"numerical", "calculated", "calculatedsimple"}
            else "short_answer"
        )
        question["input_name"] = text_inputs.first.get_attribute("name") or ""
    else:
        raise RuntimeError(f"Unsupported quiz question controls: classes={sorted(classes)}")
    return question


def fill_quiz_answer(container, question: dict, solution: dict) -> None:
    question_type = question["type"]
    if question_type in {"numerical", "short_answer"}:
        container.locator(f'[name="{question["input_name"]}"]').fill(str(solution["answer"]))
    elif question_type == "single_choice":
        value = solution["choice_value"]
        container.locator(
            f'input[type="radio"][name*="_answer"][value="{value}"]'
        ).check()
    elif question_type == "multi_choice":
        selected = set(solution["choice_names"])
        for choice in question["choices"]:
            control = container.locator(
                f'input[type="checkbox"][name="{choice["name"]}"]'
            )
            if choice["name"] in selected:
                control.check()
            elif control.is_checked():
                control.uncheck()
    elif question_type == "matching":
        for name, value in solution["matches"].items():
            container.locator(f'select[name="{name}"]').select_option(value)
    else:
        raise RuntimeError(f"Unsupported quiz question type: {question_type}")
