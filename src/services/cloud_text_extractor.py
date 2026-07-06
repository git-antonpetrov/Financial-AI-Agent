import os
import io
from pdf2image import convert_from_path
from google.cloud import vision

def extract_text_cloud(pdf_path: str, pages_count: int = 1) -> str:
    """
    Extracts text from a PDF file using Google Cloud Vision API OCR.
    Converts the specified number of pages to images and sends them to the API.
    Uses ADC for authorization.
    """
    try:
        # Resolve poppler path. If poppler is installed via conda or in the project root.
        # Fallback to None lets pdf2image use system PATH.
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        poppler_path = os.path.join(project_root, "poppler", "Library", "bin")
        if not os.path.exists(poppler_path):
            poppler_path = None
            
        images = convert_from_path(pdf_path, first_page=1, last_page=pages_count, poppler_path=poppler_path)
    except Exception as e:
        raise RuntimeError(f"Failed to convert PDF to images: {e}")

    client = vision.ImageAnnotatorClient()
    full_text = []

    for image in images:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        content = img_byte_arr.getvalue()
        
        vision_image = vision.Image(content=content)
        # Using Google Vision OCR (not Document AI)
        response = client.document_text_detection(image=vision_image)
        
        if response.error.message:
            raise Exception(f"Vision API error: {response.error.message}")
            
        if response.full_text_annotation:
            full_text.append(response.full_text_annotation.text)

    return "\n\n".join(full_text)
