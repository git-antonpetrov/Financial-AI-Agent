import os
import sys
import asyncio
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from src.core.app_clients import AppClients
from src.core.models import OrchestratorRun
from sqlalchemy import select

async def main():
    print("Testing DB connection...")
    try:
        # Load environment variables if needed
        from dotenv import load_dotenv
        load_dotenv()

        print("Initializing DB...")
        await AppClients.init_db()
        print("DB initialized successfully.")

        session_maker = AppClients.get_async_session()
        print("Async session maker created.")

        async with session_maker() as session:
            # Try to query something
            result = await session.execute(select(OrchestratorRun).limit(1))
            run = result.scalar_one_or_none()
            print(f"Query successful. Found OrchestratorRun: {run}")
            
    except Exception as e:
        print(f"Database test failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
