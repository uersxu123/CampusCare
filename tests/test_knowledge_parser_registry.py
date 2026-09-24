import unittest

from app.services.knowledge_ingestion.parser_registry import ParserRegistry


class ParserRegistryTests(unittest.TestCase):
    def test_registry_routes_supported_types_and_rejects_unknown_binary(self):
        registry = ParserRegistry.default()
        self.assertEqual(registry.resolve("text/plain", "a.txt").name, "plaintext")
        self.assertEqual(registry.resolve("text/markdown", "a.md").name, "markdown-it")
        self.assertEqual(registry.resolve("application/pdf", "a.pdf").name, "pdf-auto")
        with self.assertRaises(ValueError):
            registry.resolve("application/octet-stream", "a.doc")


if __name__ == "__main__":
    unittest.main()
