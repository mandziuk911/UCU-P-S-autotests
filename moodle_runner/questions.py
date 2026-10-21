"""Parsing and filling Moodle Lesson question forms."""

from __future__ import annotations

import hashlib


QUESTION_MARKERS = {
    "_qf__lesson_display_answer_form_numerical": "numerical",
    "_qf__lesson_display_answer_form_multichoice_singleanswer": "single_choice",
    "_qf__lesson_display_answer_form_truefalse": "single_choice",
    "_qf__lesson_display_answer_form_multichoice_multianswer": "multi_choice",
    "_qf__lesson_display_answer_form_matching": "matching",
    "_qf__lesson_display_answer_form_shortanswer": "short_answer",
}


def find_question_form(page):
    forms = page.locator('form[action*="/mod/lesson/continue.php"]')
    for index in range(forms.count()):
        form = forms.nth(index)
        names = form.locator('input[type="hidden"]').evaluate_all(
            "elements => elements.map(element => element.name)"
        )
        if any(name in QUESTION_MARKERS for name in names):
            return form
        unsupported = [
            name for name in names if name.startswith("_qf__lesson_display_answer_form_")
        ]
        if unsupported:
            raise RuntimeError(
                f"Unsupported Moodle Lesson question form: {unsupported}; url={page.url}"
            )
    return None


def parse_question(form) -> dict:
    hidden_names = form.locator('input[type="hidden"]').evaluate_all(
        "elements => elements.map(element => element.name)"
    )
    question_type = next(
        (QUESTION_MARKERS[name] for name in hidden_names if name in QUESTION_MARKERS),
        None,
    )
    if not question_type:
        raise RuntimeError("Unsupported Moodle Lesson question form")

    page_id = form.locator('input[name="pageid"]').input_value()
    stem_locator = form.locator("#id_pageheadercontainer .contents").first
    stem = " ".join(stem_locator.inner_text().split())
    question_id = hashlib.sha256(f"{page_id}:{stem}".encode()).hexdigest()[:16]
    question: dict = {
        "question_id": question_id,
        "page_id": page_id,
        "type": question_type,
        "stem": stem,
    }

    if question_type == "single_choice":
        choices = []
        radios = form.locator('input[type="radio"][name="answerid"]')
        for index in range(radios.count()):
            radio = radios.nth(index)
            value = radio.get_attribute("value") or ""
            text = radio.evaluate(
                "element => element.labels?.[0]?.innerText "
                "|| element.closest('label')?.innerText "
                "|| element.parentElement?.innerText || ''"
            )
            text = " ".join((text or value).split())
            choices.append({"value": value, "text": text})
        question["choices"] = choices

    if question_type == "multi_choice":
        choices = []
        checkboxes = form.locator('input[type="checkbox"][name^="answer["]')
        for index in range(checkboxes.count()):
            checkbox = checkboxes.nth(index)
            name = checkbox.get_attribute("name") or ""
            text = checkbox.evaluate(
                "element => element.labels?.[0]?.innerText "
                "|| element.closest('label')?.innerText "
                "|| element.parentElement?.innerText || ''"
            )
            choices.append({"name": name, "text": " ".join((text or name).split())})
        if not choices:
            raise RuntimeError("Multiple-answer question has no answer checkboxes")
        question["choices"] = choices

    if question_type == "matching":
        rows = []
        selects = form.locator('select[name^="response["]')
        for index in range(selects.count()):
            select = selects.nth(index)
            name = select.get_attribute("name") or ""
            label_text = select.evaluate(
                "element => element.labels?.[0]?.innerText "
                "|| element.closest('.fitem')?.querySelector('label')?.innerText || ''"
            )
            label_text = " ".join((label_text or name).split())
            options = select.locator("option").evaluate_all(
                "options => options.filter(o => o.value).map(o => "
                "({value: o.value, text: o.textContent.trim()}))"
            )
            rows.append({"name": name, "label": label_text, "options": options})
        question["rows"] = rows

    return question


def fill_answer(form, question: dict, solution: dict) -> None:
    question_type = question["type"]
    if question_type in {"numerical", "short_answer"}:
        form.locator('input[name="answer"]').fill(str(solution["answer"]))
    elif question_type == "single_choice":
        form.locator(
            f'input[type="radio"][name="answerid"][value="{solution["choice_value"]}"]'
        ).check()
    elif question_type == "multi_choice":
        selected = set(solution["choice_names"])
        offered = {choice["name"] for choice in question["choices"]}
        if not selected or not selected <= offered:
            raise RuntimeError("Invalid multiple-answer checkbox selections")
        for choice in question["choices"]:
            form.locator(
                f'input[type="checkbox"][name="{choice["name"]}"]'
            ).set_checked(choice["name"] in selected)
    elif question_type == "matching":
        for name, value in solution["matches"].items():
            form.locator(f'select[name="{name}"]').select_option(value)
    else:
        raise RuntimeError(f"Cannot fill unsupported question type: {question_type}")
