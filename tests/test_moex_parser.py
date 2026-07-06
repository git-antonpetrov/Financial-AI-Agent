import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.elt.extract.parsers.moex_parser import MoexParser

class TestMoexParser(unittest.TestCase):
    def test_parser_fetch_and_download(self):
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        parser = MoexParser(base_dir)
        
        # Test just the first URL to verify it runs without crashing and uploads to MinIO
        # Using a slice to avoid a long full test during CI/testing phase
        original_urls = parser.target_urls
        parser.target_urls = [original_urls[0]]
        
        try:
            has_new = parser.fetch_and_download()
            print(f"Test executed successfully. has_new={has_new}")
        except Exception as e:
            self.fail(f"fetch_and_download raised an exception: {e}")
        finally:
            # Restore
            parser.target_urls = original_urls

if __name__ == '__main__':
    unittest.main()
