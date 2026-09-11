import os
import unittest
import shutil
import tempfile
from src.services.document_to_pdf import convert_to_pdf
class TestDocumentToPDF(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.txt_path = os.path.join(self.test_dir, "test.txt")
        with open(self.txt_path, "w", encoding="utf-8") as f:
            f.write("Hello, World!\nThis is a test text file.")
        self.md_path = os.path.join(self.test_dir, "test.md")
        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write("# Hello\n\n**This is markdown**\n\n- Item 1\n- Item 2")
        self.rtf_path = os.path.join(self.test_dir, "test.rtf")
        with open(self.rtf_path, "w", encoding="utf-8") as f:
            f.write(r"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0\fswiss\fcharset0 Helvetica;}}{\colortbl;\red0\green0\blue0;}\cf1\f0\fs24 Hello World!}")
    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
    def test_convert_txt(self):
        pdf_path = convert_to_pdf(self.txt_path, self.test_dir)
        self.assertTrue(os.path.exists(pdf_path))
        self.assertTrue(pdf_path.endswith(".pdf"))
        self.assertEqual(os.path.basename(pdf_path), "test.pdf")
    def test_convert_md(self):
        pdf_path = convert_to_pdf(self.md_path, self.test_dir)
        self.assertTrue(os.path.exists(pdf_path))
        self.assertTrue(pdf_path.endswith(".pdf"))
        self.assertEqual(os.path.basename(pdf_path), "test.pdf")
    def test_convert_rtf(self):
        pdf_path = convert_to_pdf(self.rtf_path, self.test_dir)
        self.assertTrue(os.path.exists(pdf_path))
        self.assertTrue(pdf_path.endswith(".pdf"))
        self.assertEqual(os.path.basename(pdf_path), "test.pdf")
if __name__ == "__main__":
    unittest.main()
