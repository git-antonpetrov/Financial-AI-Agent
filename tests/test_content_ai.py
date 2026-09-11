import os
import json
from src.elt.utils.content_ai_recognizer import ContentCaptureRecognizer
def main():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    pdf_path = os.path.join(project_root, "dummy.pdf")
    print(f"Testing ContentCaptureRecognizer on {pdf_path}")
    try:
        recognizer = ContentCaptureRecognizer(
            delete_batch_after=True 
        )
        result_dict = recognizer.recognize(pdf_path)
        print("\n--- Recognition Result ---")
        result_file = os.path.join(project_root, "result.json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result_dict, f, indent=4, ensure_ascii=False)
        print(f"Result saved to {result_file}")
        print("--------------------------\n")
        print("Success! The nested dictionary was formed correctly.")
    except Exception as e:
        print(f"Error during recognition: {e}")
if __name__ == "__main__":
    main()
