import asyncio
import time

async def main():
    start = time.time()
    await asyncio.sleep(7.5)
    print(f"Slept for {time.time() - start:.2f} seconds")

asyncio.run(main())
