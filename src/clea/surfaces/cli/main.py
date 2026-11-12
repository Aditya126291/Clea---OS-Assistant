"""Terminal CLI entrypoint: ``clea daemon`` and ``clea chat``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from clea.core.protocol import EventFrame, RequestFrame, ResponseFrame, parse_frame

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


async def _send_and_stream(ws_url: str) -> None:
    try:
        async with connect(ws_url) as ws:
            new_session_req = RequestFrame(method="session.new")
            await ws.send(new_session_req.model_dump_json())
            raw_init = await ws.recv()
            init_frame = parse_frame(raw_init)
            if not isinstance(init_frame, ResponseFrame) or not init_frame.ok:
                print("Failed to create session with daemon.", file=sys.stderr)
                return

            session_id = init_frame.session_id
            print(f"Connected to Clea ({ws_url}) [session {session_id[:8]}]")
            print("Type a message and press Enter. Type /exit or Ctrl+C to quit.")

            while True:
                try:
                    user_text = await asyncio.to_thread(input, "\nYou: ")
                except (EOFError, KeyboardInterrupt):
                    print("\nBye.")
                    break

                stripped = user_text.strip()
                if not stripped:
                    continue
                if stripped.lower() in {"/exit", "/quit"}:
                    print("Bye.")
                    break

                req = RequestFrame(
                    method="chat.send",
                    session_id=session_id,
                    params={"text": stripped},
                )
                await ws.send(req.model_dump_json())

                print("Clea: ", end="", flush=True)
                saw_delta = False

                while True:
                    raw = await ws.recv()
                    frame = parse_frame(raw)

                    if isinstance(frame, EventFrame):
                        if frame.event == "text.delta":
                            chunk = str(frame.data.get("text", ""))
                            if chunk:
                                print(chunk, end="", flush=True)
                                saw_delta = True
                        elif frame.event == "tool.start":
                            tool_name = frame.data.get("name", "tool")
                            print(f"\n[running tool: {tool_name}...]", flush=True)
                            print("Clea: ", end="", flush=True)
                        elif frame.event == "tool.end":
                            if frame.data.get("is_error"):
                                print(f"\n[tool error: {frame.data.get('output')}]", flush=True)
                                print("Clea: ", end="", flush=True)
                        elif frame.event == "error":
                            print(f"\n[error: {frame.data.get('message')}]", file=sys.stderr)
                    elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                        if not frame.ok:
                            print(f"\n[error: {frame.error}]", file=sys.stderr)
                        elif not saw_delta:
                            reply = str(frame.result.get("text", ""))
                            print(reply, end="", flush=True)
                        print()
                        break
    except (OSError, WebSocketException) as exc:
        print(
            f"Could not connect to Clea daemon at {ws_url} ({exc}).\n"
            "Start the daemon first in another terminal with: clea daemon",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def _run_daemon_cmd(host: str, port: int) -> None:
    from clea.core.daemon import get_daemon_info, run_daemon

    info = get_daemon_info()
    print(f"Starting Clea daemon on ws://{host}:{port}")
    print(f"Data directory: {info['data_dir']}")
    print(f"Today's daily log: {info['daily_log']}")
    try:
        run_daemon(host=host, port=port)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="clea", description="Clea personal AI assistant CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Start the localhost Clea daemon")
    daemon_parser.add_argument(
        "--host",
        default=os.environ.get("CLEA_HOST", DEFAULT_HOST),
        help="Bind address (localhost only, default: 127.0.0.1)",
    )
    daemon_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CLEA_PORT", DEFAULT_PORT)),
        help="WebSocket port (default: 8765)",
    )

    chat_parser = subparsers.add_parser("chat", help="Connect to the running Clea daemon")
    chat_parser.add_argument(
        "--url",
        default=None,
        help="Daemon WebSocket URL (default: ws://127.0.0.1:8765)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for ``clea``."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "daemon":
        _run_daemon_cmd(args.host, args.port)
    elif args.command == "chat":
        host = os.environ.get("CLEA_HOST", DEFAULT_HOST)
        port = int(os.environ.get("CLEA_PORT", DEFAULT_PORT))
        ws_url = args.url or f"ws://{host}:{port}"
        asyncio.run(_send_and_stream(ws_url))
