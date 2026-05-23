from __future__ import annotations

import asyncio
import logging

from telepath.config import load_settings
from telepath.manager_bot import run_manager_bot
from telepath.user_client import run_user_client


async def run() -> None:
    settings = load_settings()
    tasks = [
        asyncio.create_task(run_user_client(settings)),
        asyncio.create_task(run_manager_bot(settings)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
