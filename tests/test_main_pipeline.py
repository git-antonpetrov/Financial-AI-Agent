import os
import sys
import asyncio
import io
sys.stdout.reconfigure(encoding='utf-8')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.core.app_clients import AppClients
from src.elt.load.main_pipeline import MainPipeline
async def run_test():
    print("\n--- TEST: Main Pipeline ---\n")
    minio_client = AppClients.get_minio_client()
    raw_bucket = "raw-documents"
    if not minio_client.bucket_exists(raw_bucket):
        minio_client.make_bucket(raw_bucket)
    test_filename = "test_document_for_pipeline.txt"
    test_content = (
        "Федеральный закон\n"
        "О противодействии коррупции и отмыванию доходов.\n"
        "Настоящий закон признает утратившим силу федеральный закон от 01.01.2000 N 1-ФЗ.\n"
        "Документ подписан 15.05.2023.\n\n"
        "Этот текст предназначен для теста.\n"
    ).encode("utf-8")
    print(f"Uploading {test_filename} to {raw_bucket}...")
    minio_client.put_object(
        raw_bucket, 
        test_filename, 
        io.BytesIO(test_content), 
        length=len(test_content)
    )
    pipeline = MainPipeline()
    await pipeline.run()
    print("\nCheck if file was removed from raw-documents...")
    try:
        minio_client.stat_object(raw_bucket, test_filename)
        print("ERROR: File still exists in raw-documents.")
    except Exception:
        print("SUCCESS: File was removed from raw-documents.")
    print("\nTest finished.")
if __name__ == "__main__":
    asyncio.run(run_test())
