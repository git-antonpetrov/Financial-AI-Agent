import asyncio
class TestClass:
    def __init__(self):
        self.sem = asyncio.Semaphore(5)
    async def run(self):
        print("Acquiring sem...")
        async with self.sem:
            print("Acquired sem!")
if __name__ == "__main__":
    t = TestClass()
    asyncio.run(t.run())
