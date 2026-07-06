import asyncio

class TestClass:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def run(self):
        print("Acquiring...")
        async with self.lock:
            print("Acquired!")

if __name__ == "__main__":
    t = TestClass()
    asyncio.run(t.run())
