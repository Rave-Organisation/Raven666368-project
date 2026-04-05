import asyncio
import json
import os
import websockets
from dataclasses import dataclass
from dotenv import load_dotenv

from engine.rug_checks import CandidateMint, rug_check_worker

load_dotenv()

HELIUS_WSS_URL = os.getenv("HELIUS_RPC_URL", "").replace("https", "wss")
PUMP_PROGRAM_ID = "6EF8rrecthR5DkwiJvK9vXy3vf98aM1eX3r4r"


async def helius_listener(ws_url: str, raw_queue: asyncio.Queue):
    async with websockets.connect(ws_url) as ws:
        print("Listener connected and watching...")

        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
                await raw_queue.put(msg)
            except json.JSONDecodeError:
                continue


async def filter_worker(raw_queue: asyncio.Queue, cand_queue: asyncio.Queue):
    print("Filter worker ready...")
    while True:
        msg = await raw_queue.get()
        logs = msg.get("params", {}).get("result", {}).get("logs", [])
        program_id = msg.get("params", {}).get("result", {}).get("value", {}).get("programId")

        if program_id == PUMP_PROGRAM_ID and any("InitializeMint" in l for l in logs):
            print("New Mint Detected!")
            new_candidate = CandidateMint(
                mint="SampleMint...",
                creator="SampleCreator...",
                tx_sig="SampleSig..."
            )
            await cand_queue.put(new_candidate)

        raw_queue.task_done()


async def main():
    print("Starting Bot Architecture...")

    raw_log_queue = asyncio.Queue(maxsize=10000)
    candidate_queue = asyncio.Queue()
    buy_queue = asyncio.Queue()

    asyncio.create_task(filter_worker(raw_log_queue, candidate_queue))
    asyncio.create_task(rug_check_worker(candidate_queue, buy_queue))

    await helius_listener(HELIUS_WSS_URL, raw_log_queue)


if __name__ == "__main__":
    asyncio.run(main())
