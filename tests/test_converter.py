from src.utils.document_to_pdf import convert_to_pdf
import sys

try:
    print("Testing LibreOffice converter...")
    pdf_path = convert_to_pdf(r"C:\Users\anton\source\repos\Financial-AI-Agent\README.md", r"C:\Users\anton\Downloads")
    print("SUCCESS, PDF created at:", pdf_path)
except Exception as e:
    print("FAILED:", e)
    sys.exit(1)
