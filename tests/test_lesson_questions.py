"""Offline regression tests for Lesson form classification."""

import unittest

from playwright.sync_api import sync_playwright

from moodle_runner.questions import fill_answer, find_question_form, parse_question
from moodle_runner.pi_solver import _validate


class LessonQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_truefalse_is_parsed_as_radio_choices(self):
        self.page.set_content('''
            <form action="https://learn.ucu.edu.ua/mod/lesson/continue.php">
                <input type="hidden" name="_qf__lesson_display_answer_form_truefalse" value="1">
                <input type="hidden" name="pageid" value="2718">
                <div id="id_pageheadercontainer"><div class="contents">Example statement</div></div>
                <label><input type="radio" name="answerid" value="41">True</label>
                <label><input type="radio" name="answerid" value="42">False</label>
                <input type="submit" name="submitbutton" value="Відправити">
            </form>
        ''')
        form = find_question_form(self.page)
        self.assertIsNotNone(form)
        question = parse_question(form)
        self.assertEqual(question["type"], "single_choice")
        self.assertEqual(question["page_id"], "2718")
        self.assertEqual(question["choices"], [
            {"value": "41", "text": "True"},
            {"value": "42", "text": "False"},
        ])

    def test_multianswer_parses_validates_and_updates_checkboxes(self):
        self.page.set_content('''
            <form action="https://learn.ucu.edu.ua/mod/lesson/continue.php">
                <input type="hidden" name="_qf__lesson_display_answer_form_multichoice_multianswer" value="1">
                <input type="hidden" name="pageid" value="2748">
                <div id="id_pageheadercontainer"><div class="contents">Example choices</div></div>
                <label><input type="checkbox" name="answer[11]" value="1">Alpha</label>
                <label><input type="checkbox" name="answer[12]" value="1" checked>Beta</label>
                <label><input type="checkbox" name="answer[13]" value="1">Gamma</label>
                <input type="submit" name="submitbutton" value="Відправити">
            </form>
        ''')
        form = find_question_form(self.page)
        question = parse_question(form)
        self.assertEqual(question["type"], "multi_choice")
        self.assertEqual(question["choices"], [
            {"name": "answer[11]", "text": "Alpha"},
            {"name": "answer[12]", "text": "Beta"},
            {"name": "answer[13]", "text": "Gamma"},
        ])
        solution = _validate(question, {
            "choice_texts": ["Alpha", "Gamma"], "confidence": 1,
        })
        fill_answer(form, question, solution)
        self.assertTrue(form.locator('[name="answer[11]"]').is_checked())
        self.assertFalse(form.locator('[name="answer[12]"]').is_checked())
        self.assertTrue(form.locator('[name="answer[13]"]').is_checked())
        with self.assertRaisesRegex(RuntimeError, "Invalid multiple-answer"):
            fill_answer(form, question, {"choice_names": ["answer[99]"]})
        self.assertTrue(form.locator('[name="answer[11]"]').is_checked())

    def test_unknown_question_type_is_not_treated_as_navigation(self):
        self.page.set_content('''
            <form action="https://learn.ucu.edu.ua/mod/lesson/continue.php">
                <input type="hidden" name="_qf__lesson_display_answer_form_unknown" value="1">
                <input type="submit" value="Відправити">
            </form>
        ''')
        with self.assertRaisesRegex(RuntimeError, "Unsupported Moodle Lesson question form"):
            find_question_form(self.page)

    def test_content_navigation_is_not_a_question(self):
        self.page.set_content('''
            <form action="https://learn.ucu.edu.ua/mod/lesson/continue.php">
                <input type="submit" value="Next">
            </form>
        ''')
        self.assertIsNone(find_question_form(self.page))


if __name__ == "__main__":
    unittest.main()
