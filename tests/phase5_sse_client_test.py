import asyncio
import sys

import httpx


async def main():
    url = "http://127.0.0.1:8004/api/events"
    received = []
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", url) as response:
            print(f"connected: HTTP {response.status_code}")
            buffer = ""
            deadline = asyncio.get_event_loop().time() + 12
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    if frame.strip() and not frame.startswith(":"):
                        received.append(frame)
                        print(f"  received frame #{len(received)}: {frame[:150]}")
                if asyncio.get_event_loop().time() > deadline or len(received) >= 6:
                    break
    print(f"\nTotal real SSE frames received: {len(received)}")
    if len(received) == 0:
        print("FAIL: no events received")
        sys.exit(1)
    else:
        print("PASS: live events were actually received over the SSE stream")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
