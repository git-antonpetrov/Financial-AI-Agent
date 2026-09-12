import asyncio
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from src.core.clients.db import get_async_session_maker
from src.core.clients.storage import get_minio_client
from src.core.clients.llm import get_cloud_ai_client
from src.elt.load.main_pipeline import MainPipeline
from src.elt.transform.chromadb_upsert import ChromaDBUpsert
from src.elt.transform.chromadb_delete import ChromaDBDelete

async def run_test():
    db_maker = get_async_session_maker()
    minio = get_minio_client()
    llm = get_cloud_ai_client()

    backup_dir = r"C:\Users\anton\Downloads\S3_RAG_Backup"
    if not os.path.exists(backup_dir):
        print(f"Directory {backup_dir} does not exist.")
        return
        
    all_files = [f for f in os.listdir(backup_dir) if f.lower().endswith('.pdf')]
    test_files = all_files[:2] # Pick first 2 files
    
    print(f"Uploading test files to MinIO raw-documents: {test_files}")
    for filename in test_files:
        filepath = os.path.join(backup_dir, filename)
        with open(filepath, "rb") as f:
            content = f.read()
            minio.put_object(
                "raw-documents",
                filename,
                data=import_io_BytesIO(content),
                length=len(content),
                content_type="application/pdf"
            )
            print(f"Uploaded {filename} to raw-documents.")

    print("\nRunning Main Pipeline...")
    main_pipeline = MainPipeline(db_maker, minio, llm)
    await main_pipeline.run()

    from src.core.clients.vector_db import get_chroma_client
    from src.core.clients.llm import get_embedder
    chroma_client = get_chroma_client()
    embedder = get_embedder()

    print("\nRunning ChromaDB Transform Upsert...")
    chroma_upsert = ChromaDBUpsert(db_maker, minio, chroma_client, embedder)
    await chroma_upsert.run()

    print("\nRunning ChromaDB Transform Delete...")
    chroma_delete = ChromaDBDelete(db_maker, minio, chroma_client, embedder)
    await chroma_delete.run()

    print("\nTest pipeline completed.")

def import_io_BytesIO(content):
    import io
    return io.BytesIO(content)

if __name__ == "__main__":
    asyncio.run(run_test())
