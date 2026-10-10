#!/usr/bin/python3
"""Standard-library bridge inside a generated project's isolated network namespace."""

import asyncio
import os
import re
import signal
import stat
import sys
from contextlib import suppress
from pathlib import Path


async def main(mode, socket_path, raw_port):
    path = Path(socket_path)
    port = int(raw_port)
    if mode not in {"preview", "market"} or not 1 <= port <= 65535 or not re.fullmatch(r"/tmp/qp-preview/[a-f0-9]{20}/[pm]\.sock", str(path)):
        raise ValueError("Invalid bridge mode, runtime socket or port")
    stopping, tasks = asyncio.Event(), set()

    async def pump(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        with suppress(OSError, NotImplementedError):
            writer.write_eof()

    async def accept(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        upstream = None
        try:
            if len(tasks) > 512:
                return
            async with asyncio.timeout(3):
                other, upstream = await (asyncio.open_connection("127.0.0.1", port) if mode == "preview" else asyncio.open_unix_connection(str(path)))
            await asyncio.gather(pump(reader, upstream), pump(other, writer))
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            tasks.discard(task)

    if mode == "preview":
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError("Refusing to replace a non-socket bridge path")
            path.unlink()
        server = await asyncio.start_unix_server(accept, str(path))
        os.chmod(path, 0o600)
    else:
        server = await asyncio.start_server(accept, "127.0.0.1", port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    try:
        await stopping.wait()
    finally:
        server.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
        if mode == "preview":
            with suppress(FileNotFoundError):
                if stat.S_ISSOCK(path.lstat().st_mode):
                    path.unlink()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: preview-network-bridge.py <preview|market> <socket> <port>")
    asyncio.run(main(*sys.argv[1:]))
