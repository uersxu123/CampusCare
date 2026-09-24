import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StudentMarkdownContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "app" / "static" / "student.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "app" / "static" / "student.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    def test_local_markdown_and_sanitizer_load_before_student_script(self):
        marked = self.html.index('/vendor/marked.min.js')
        purify = self.html.index('/vendor/purify.min.js')
        student = self.html.index('/student.js')

        self.assertLess(marked, student)
        self.assertLess(purify, student)
        self.assertNotIn("cdn.jsdelivr.net", self.html)
        self.assertNotIn("unpkg.com", self.html)

    def test_user_and_streaming_content_remain_plain_text(self):
        self.assertIn("bubble.textContent = content;", self.script)
        self.assertIn('assistant.textContent = eventData.content || "";', self.script)

    def test_history_and_verified_completion_use_one_safe_renderer(self):
        self.assertIn("function renderAssistantMarkdown(element, markdown)", self.script)
        self.assertIn("renderMarkdown: role === \"assistant\"", self.script)
        self.assertIn(
            'terminalStatus === "COMPLETED" && completionVerified',
            self.script,
        )
        self.assertIn("renderAssistantMarkdown(assistant", self.script)

    def test_sanitizer_contract_forbids_dangerous_markup_and_protocols(self):
        self.assertIn("DOMPurify.sanitize", self.script)
        for tag in ("script", "style", "iframe", "object", "embed", "form", "input", "button", "textarea", "svg", "math"):
            self.assertIn(f'"{tag}"', self.script)
        self.assertIn('"style"', self.script)
        self.assertIn('"javascript:"', self.script)
        self.assertIn('"data:"', self.script)
        self.assertNotIn("element.innerHTML", self.script)

    def test_markdown_styles_are_scoped_to_assistant_bubbles(self):
        self.assertIn(".message.assistant .bubble", self.styles)
        self.assertIn(".message.assistant .bubble table", self.styles)
        self.assertIn(".message.assistant .bubble pre", self.styles)
        self.assertIn("overflow-x: auto", self.styles)


if __name__ == "__main__":
    unittest.main()
