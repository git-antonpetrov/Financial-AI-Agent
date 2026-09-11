import os
import unittest
import shutil
import tempfile
import json
from src.etl.storage_manager import StorageManager
class TestStorageManagerLogic(unittest.TestCase):
    def setUp(self):
        self.manager = StorageManager()
        self.manager.landing_dir = tempfile.mkdtemp()
        self.manager.storage_dir = tempfile.mkdtemp()
        self.manager.tracker_file = os.path.join(self.manager.storage_dir, "test_tracker.json")
        with open(self.manager.tracker_file, "w") as f:
            json.dump({}, f)
    def tearDown(self):
        shutil.rmtree(self.manager.landing_dir, ignore_errors=True)
        shutil.rmtree(self.manager.storage_dir, ignore_errors=True)
    def test_sanitize_filename(self):
        self.assertEqual(self.manager._sanitize_filename("test document.pdf"), "test_document_00000000.pdf")
        self.assertEqual(self.manager._sanitize_filename("test_doc_15042023.pdf"), "test_doc_15042023.pdf")
        self.assertEqual(self.manager._sanitize_filename("some_file"), "some_file_00000000.pdf")
    def test_parse_date(self):
        self.assertEqual(self.manager._parse_date_from_filename("fz_39_22042024.pdf"), 20240422)
        self.assertEqual(self.manager._parse_date_from_filename("doc_01012020.pdf"), 20200101)
        self.assertEqual(self.manager._parse_date_from_filename("no_date.pdf"), 0)
        self.assertEqual(self.manager._parse_date_from_filename("bad_00000000.pdf"), 0)
    def test_duplicate_resolution(self):
        old_file = os.path.join(self.manager.storage_dir, "document_15042023.pdf")
        with open(old_file, "w") as f:
            f.write("old version")
        import glob
        import re
        base_name_without_date = "document"
        new_filename = "document_16042023.pdf"
        new_date_val = self.manager._parse_date_from_filename(new_filename)
        existing_files = glob.glob(os.path.join(self.manager.storage_dir, f"{base_name_without_date}*.pdf"))
        should_move = True
        tracker_data = {}
        tracker_data["document_15042023.pdf"] = "oldhash"
        for existing_path in existing_files:
            existing_name = os.path.basename(existing_path)
            if re.search(rf'^{re.escape(base_name_without_date)}_\d{ 8} \.pdf$', existing_name):
                existing_date_val = self.manager._parse_date_from_filename(existing_name)
                if new_date_val > existing_date_val:
                    os.remove(existing_path)
                    if existing_name in tracker_data:
                        del tracker_data[existing_name]
                else:
                    should_move = False
                    break
        self.assertTrue(should_move)
        self.assertFalse(os.path.exists(old_file))
        self.assertNotIn("document_15042023.pdf", tracker_data)
        with open(os.path.join(self.manager.storage_dir, "document_18042023.pdf"), "w") as f:
            f.write("newer version")
        new_filename = "document_17042023.pdf"
        new_date_val = self.manager._parse_date_from_filename(new_filename)
        existing_files = glob.glob(os.path.join(self.manager.storage_dir, f"{base_name_without_date}*.pdf"))
        should_move = True
        for existing_path in existing_files:
            existing_name = os.path.basename(existing_path)
            if re.search(rf'^{re.escape(base_name_without_date)}_\d{ 8} \.pdf$', existing_name):
                existing_date_val = self.manager._parse_date_from_filename(existing_name)
                if new_date_val > existing_date_val:
                    pass
                else:
                    should_move = False
                    break
        self.assertFalse(should_move) 
        self.assertTrue(os.path.exists(os.path.join(self.manager.storage_dir, "document_18042023.pdf")))
if __name__ == "__main__":
    unittest.main()
