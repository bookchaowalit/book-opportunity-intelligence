import py_compile
import unittest
from pathlib import Path


class SmokeTest(unittest.TestCase):
    def test_package_compiles(self):
        root = Path(__file__).parents[1] / "src" / "book_opportunities"
        for path in sorted(root.glob("*.py")):
            py_compile.compile(str(path), doraise=True)

    def test_domain_fixture_files_present(self):
        fixtures = Path(__file__).parents[1] / "fixtures" / "domain-products"
        expected = {
            "crypto",
            "stocks",
            "fx",
            "defi",
            "flights",
            "seo",
            "ai_tools",
        }
        present = {p.stem for p in fixtures.glob("*.json")}
        self.assertEqual(expected, present)


if __name__ == "__main__":
    unittest.main()
