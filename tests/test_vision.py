from src.elt.utils.cloud_text_extractor import extract_text_cloud
import sys
try:
    print("Testing Google Vision OCR...")
    text = extract_text_cloud(r"C:\Users\anton\Downloads\fz_o_federalnom_byudzhete_2025_11_26.pdf", pages_count=1)
    print("SUCCESS, extracted text length:", len(text))
except Exception as e:
    print("FAILED:", e)
    sys.exit(1)
