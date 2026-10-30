"""Session-aware WebSocket relay so logout/expiry revokes live ttyd sessions."""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import connect

from .deps import optional_session
from .ingress import origin_allowed

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/term/ws")
async def terminal(websocket: WebSocket):
    session = optional_session(websocket)
    if (
        session is None or not websocket.app.state.config.ttyd_enabled
        or not websocket.headers.get("origin") or not origin_allowed(websocket)
    ):
        await websocket.close(code=1008)
        return
    connector = getattr(websocket.app.state, "terminal_connector", connect)
    port = websocket.app.state.config.ttyd_port
    tasks = []
    try:
        async with connector(
            f"ws://127.0.0.1:{port}/term/ws",
            subprotocols=["tty"], open_timeout=3, max_size=2 * 1024 * 1024,
        ) as backend:
            await websocket.accept(subprotocol="tty")

            async def browser_to_ttyd():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await backend.send(message.get("bytes") if message.get("bytes") is not None
                                       else message["text"])

            async def ttyd_to_browser():
                async for data in backend:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            async def watch_session():
                while optional_session(websocket) is session:
                    await asyncio.sleep(1)
                await websocket.close(code=1008)

            tasks = [asyncio.create_task(coro) for coro in (
                browser_to_ttyd(), ttyd_to_browser(), watch_session()
            )]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except (OSError, InvalidHandshake, ConnectionClosed):
        logger.warning("terminal backend connection failed")
        await websocket.close(code=1011)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
