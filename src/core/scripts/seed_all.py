import asyncio
from src.server.db.seed_db import run_seed as seed_server
from src.mcp_servers.bank.db.seed_db import run_seed as seed_bank

async def main():
    print("=== Начало сидирования всех баз данных ===")
    
    print("\n1. Сидирование базы сервера (auth_db)...")
    await seed_server()
    
    print("\n2. Сидирование базы банка (bank_db)...")
    await seed_bank()
    
    print("\n=== Все базы успешно заполнены данными! ===")

if __name__ == "__main__":
    asyncio.run(main())
