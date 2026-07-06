import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.services.content_ai_recognizer import ContentCaptureRecognizer

pdf_path = r"test_3_pages.pdf"

if os.path.exists(pdf_path):
    print("Testing Content AI API with:", pdf_path)
    recognizer = ContentCaptureRecognizer()
    results = recognizer.recognize(pdf_path)
    print(f"RESULTS LENGTH: {len(results)}")
    
    # Extract strings from the parsed dict
    def extract_strings(d):
        strings = []
        if isinstance(d, dict):
            for v in d.values():
                strings.extend(extract_strings(v))
        elif isinstance(d, list):
            for item in d:
                strings.extend(extract_strings(item))
        elif isinstance(d, str):
            strings.append(d)
        return strings

    all_text = []
    for file_name, result_dict in results.items():
        print(f"--- RESULT FOR {file_name} ---")
        text_parts = extract_strings(result_dict)
        text = " ".join(text_parts)
        print(text[:1000] + ("..." if len(text) > 1000 else ""))
        all_text.append(text)
    
    # Save the raw text to recognized_text.txt in the project root
    if all_text:
        import os
        import re
        clean_text = "\n\n".join(all_text)
        # Remove excessive whitespace
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        out_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "recognized_text.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(clean_text)
        print(f"Saved recognized text to {out_path}")
else:
    print("File not found:", pdf_path)
