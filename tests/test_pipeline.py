import asyncio
from src.etl.storage_manager import StorageManager
import glob
import os

async def main():
    manager = StorageManager()
    
    files = glob.glob(os.path.join(manager.landing_dir, "*"))
    files = [f for f in files if os.path.isfile(f) and not f.endswith(".gitkeep") and f.endswith(".pdf")]
    
    if not files:
        print("No PDF files in landing dir to test.")
        return
        
    test_file = files[0]
    print(f"Testing pipeline on 1 file: {test_file}")
    
    res = await manager.process_file(test_file)
    print("Result:", res)

if __name__ == "__main__":
    asyncio.run(main())
