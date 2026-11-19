import csv
import io
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

from tidal_harness.artifacts import ArtifactStore


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.desktop = Path(tmp.name) / "Desktop"
        self.store = ArtifactStore(self.desktop)

    def test_confirmation_collision_and_repeated_click(self):
        offer = self.store.offer("复习计划.md", "# 周五\n")
        self.assertFalse(self.desktop.exists())
        self.desktop.mkdir()
        original = self.desktop / offer["name"]
        original.write_text("保留", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.store.save, [offer["id"]] * 4))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(results[0]["name"], "复习计划 (1).md")
        self.assertEqual(Path(results[0]["path"]).read_text(encoding="utf-8"), "# 周五\n")
        self.assertEqual(original.read_text(encoding="utf-8"), "保留")
        self.assertEqual(len(list(self.desktop.iterdir())), 2)

    def test_rejects_external_paths_and_unoffered_files(self):
        for name in ("../file.md", "D:\\file.txt", "CON .md", ".hidden.txt", "a.txt:extra",
                     "资料\u202etxt.md", "run.cmd", "自定义.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.offer(name, "x")
        self.assertEqual(self.store.offer("附件.exe.txt", "x")["name"], "附件-exe.txt")
        with self.assertRaises(KeyError):
            self.store.save("unknown")
        with self.assertRaises(ValueError):
            self.store.offer("数据.json", "NaN")
        self.assertFalse(self.desktop.exists())
        code = "print('hello world')\n"
        saved = self.store.save_task("abc", code)
        self.assertEqual(saved, self.store.save_task("abc", code))
        self.assertEqual(Path(saved["path"]).read_text(encoding="utf-8"), code)

    def test_csv_and_excel_treat_formulas_as_data(self):
        table = '编号,分数,备注\n001,-12,"=SUM(4,5)"\n'
        stored = self.store.offer("数据.csv", table)
        path = Path(self.store.save(stored["id"])["path"])
        self.assertEqual(list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))[1],
                         ["001", "-12", "'=SUM(4,5)"])
        book = self.store.offer("数据.xlsx", table)
        with zipfile.ZipFile(self.store.save(book["id"])["path"]) as package:
            self.assertIsNone(package.testzip())
            worksheet = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
            cells = {c.get("r"): c for c in worksheet.findall(".//{*}c")}
            self.assertEqual(cells["A2"].findtext(".//{*}t"), "001")
            self.assertEqual(cells["B2"].findtext("{*}v"), "-12")
            self.assertEqual(cells["C2"].findtext(".//{*}t"), "=SUM(4,5)")
            self.assertEqual(worksheet.findall(".//{*}f"), [])

    def test_word_contains_paragraphs_and_xml_special_characters(self):
        offered = self.store.offer("家长会.docx", "家长会\n\n周六 9:00 <报告厅> & 签到")
        with zipfile.ZipFile(self.store.save(offered["id"])["path"]) as package:
            self.assertIsNone(package.testzip())
            document = ET.fromstring(package.read("word/document.xml"))
            self.assertEqual([p.findtext(".//{*}t") for p in document.findall(".//{*}p")],
                             ["家长会", "", "周六 9:00 <报告厅> & 签到"])


if __name__ == "__main__":
    unittest.main()
