import os
import subprocess

def convert_to_pdf(input_path: str, output_dir: str) -> str:
    """
    Converts a document (.txt, .md, .rtf, .doc, .docx) to PDF using LibreOffice.
    Supports old Word 97 (.doc) format and modern formats.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        
    soffice_path = "soffice"
    if os.path.exists(r"C:\Program Files\LibreOffice\program\soffice.exe"):
        soffice_path = r"C:\Program Files\LibreOffice\program\soffice.exe"

    command = [
        soffice_path,
        "--headless",
        "--convert-to",
        "pdf",
        input_path,
        "--outdir",
        output_dir
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Error converting {input_path} to PDF: {e.stderr.decode('utf-8', errors='ignore')}")
        
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    expected_pdf = os.path.join(output_dir, f"{base_name}.pdf")
    
    if os.path.exists(expected_pdf):
        return expected_pdf
    else:
        raise FileNotFoundError(f"PDF was not generated at {expected_pdf}")
