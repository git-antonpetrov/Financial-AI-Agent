from src.services.content_ai_recognizer import ContentCaptureRecognizer
import os

pdf_path = r"C:\Users\anton\Downloads\test_large.pdf"
if not os.path.exists(pdf_path):
    with open(pdf_path, "wb") as f:
        f.write(b"0" * 300 * 1024)  # 300KB dummy file

print("Testing Content AI API with:", pdf_path)
recognizer = ContentCaptureRecognizer(delete_batch_after=False)
results = recognizer.recognize(pdf_path)
print("RESULTS LENGTH:", len(results))
