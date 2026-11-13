"""Terminal CLI entrypoint: ``clea daemon`` and ``clea chat``."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from clea.core import auth
from clea.core.protocol import (
    ApprovalDecision,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRespondParams,
    ApprovalRespondRequest,
    AssistantTokenEvent,
    BaseEvent,
    ChatSendParams,
    ChatSendRequest,
    ErrorEvent,
    ResponseFrame,
    SessionNewRequest,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    parse_frame,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _load_token_or_exit(token_file: Path | None = None) -> str:
    target = token_file or auth.token_path()
    token = auth.read_token(target)
    if not token:
        print(
            f"No daemon session token found at {target}.\n"
            "Start the daemon first in another terminal with: clea daemon",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return token


async def _prompt_approval(event: ApprovalRequestedEvent) -> ApprovalDecision:
    args_json = json.dumps(event.data.arguments)
    print(
        f"\n[approval required ({event.data.risk_level}): {event.data.tool} {args_json}]",
        flush=True,
    )
    try:
        answer = (await asyncio.to_thread(input, "Allow? [y/N]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if answer not in {"y", "yes"}:
        return "deny"
    if event.data.risk_level == "destructive":
        try:
            confirm = (
                (await asyncio.to_thread(input, "Destructive action — type 'yes' to confirm: "))
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "deny"
        if confirm != "yes":
            return "deny"
    return "allow"


async def _send_and_stream(ws_url: str, *, token: str | None = None) -> None:
    resolved_token = token or _load_token_or_exit()
    try:
        async with connect(
            ws_url,
            subprotocols=auth.client_subprotocols(resolved_token),  # type: ignore[arg-type]
        ) as ws:
            new_session_req = SessionNewRequest()
            await ws.send(new_session_req.model_dump_json())
            raw_init = await ws.recv()
            init_frame = parse_frame(raw_init)
            if not isinstance(init_frame, ResponseFrame) or not init_frame.ok:
                print("Failed to create session with daemon.", file=sys.stderr)
                return

            session_id = init_frame.session_id
            assert session_id is not None
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

                req = ChatSendRequest(
                    session_id=session_id,
                    params=ChatSendParams(text=stripped),
                )
                await ws.send(req.model_dump_json())

                print("Clea: ", end="", flush=True)
                saw_delta = False

                while True:
                    raw = await ws.recv()
                    frame = parse_frame(raw)

                    if isinstance(frame, BaseEvent):
                        if isinstance(frame, AssistantTokenEvent):
                            chunk = frame.data.text
                            if chunk:
                                print(chunk, end="", flush=True)
                                saw_delta = True
                        elif isinstance(frame, ToolCallStartedEvent):
                            print(f"\n[running tool: {frame.data.name}...]", flush=True)
                            print("Clea: ", end="", flush=True)
                        elif isinstance(frame, ToolCallFinishedEvent):
                            if frame.data.is_error:
                                print(f"\n[tool error: {frame.data.output}]", flush=True)
                                print("Clea: ", end="", flush=True)
                        elif isinstance(frame, ApprovalRequestedEvent):
                            decision = await _prompt_approval(frame)
                            await ws.send(
                                ApprovalRespondRequest(
                                    session_id=session_id,
                                    params=ApprovalRespondParams(
                                        approval_id=frame.data.approval_id,
                                        decision=decision,
                                    ),
                                ).model_dump_json()
                            )
                            print("Clea: ", end="", flush=True)
                        elif isinstance(frame, ApprovalResolvedEvent):
                            if frame.data.reason == "timeout":
                                print("\n[approval timed out -> denied]", flush=True)
                                print("Clea: ", end="", flush=True)
                        elif isinstance(frame, ErrorEvent):
                            print(f"\n[error: {frame.data.message}]", file=sys.stderr)
                    elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                        if not frame.ok:
                            print(f"\n[error: {frame.error}]", file=sys.stderr)
                        elif not saw_delta:
                            reply = str(frame.result.get("text", ""))
                            print(reply, end="", flush=True)
                        print()
                        break
    except InvalidStatus as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {401, 403}:
            print(
                f"Clea daemon rejected authentication (HTTP {status}).\n"
                "If the daemon was restarted, retry so the CLI picks up the new token.",
                file=sys.stderr,
            )
        else:
            print(f"Handshake failed connecting to {ws_url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, WebSocketException) as exc:
        print(
            f"Could not connect to Clea daemon at {ws_url} ({exc}).\n"
            "Start the daemon first in another terminal with: clea daemon",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def _run_daemon_cmd(host: str, port: int, *, background: bool = False) -> None:
    from clea.core.daemon import get_daemon_info, run_daemon

    info = get_daemon_info()
    if not background:
        print(f"Starting Clea daemon on ws://{host}:{port}")
        print(f"Data directory: {info['data_dir']}")
        print(f"Today's daily log: {info['daily_log']}")
        print(f"Token file: {info['token_file']}")
    try:
        if background:
            run_daemon(host=host, port=port, background=True)
        else:
            run_daemon(host=host, port=port)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except auth.DaemonAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except OSError as exc:
        is_addr_in_use = (
            exc.errno == errno.EADDRINUSE
            or getattr(exc, "winerror", None) == 10048
            or (exc.errno is None and "already in use" in str(exc).lower())
        )
        msg = f"Could not start Clea daemon on ws://{host}:{port} ({exc})."
        if is_addr_in_use:
            msg += "\nThe address/port may already be in use by another 'clea daemon' process."
        print(msg, file=sys.stderr)
        raise SystemExit(1) from exc


def _run_autostart_cmd(action: str) -> None:
    from clea.core.daemon import daemon_log_path
    from clea.surfaces.cli.autostart import (
        install_autostart,
        status_autostart,
        uninstall_autostart,
    )

    try:
        if action == "install":
            status = install_autostart()
            print(f"Installed Windows logon task '{status.task_name}'.")
            print(f"Command: {status.command}")
            print(f"Working directory: {status.working_dir}")
            print(f"Daemon log: {daemon_log_path()}")
        elif action == "uninstall":
            removed = uninstall_autostart()
            if removed:
                print("Removed Windows logon task 'CleaDaemon'.")
            else:
                print("Windows logon task 'CleaDaemon' is not installed.")
        elif action == "status":
            status = status_autostart()
            if not status.installed:
                print(f"Task '{status.task_name}': not installed")
            else:
                print(f"Task '{status.task_name}': installed ({status.state or 'Ready'})")
                if status.command:
                    print(f"Command: {status.command}")
                if status.working_dir:
                    print(f"Working directory: {status.working_dir}")
                print(f"Daemon log: {daemon_log_path()}")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
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
    daemon_parser.add_argument(
        "--background",
        action="store_true",
        help="Log to a rotating file in the user data directory instead of stdout",
    )

    chat_parser = subparsers.add_parser("chat", help="Connect to the running Clea daemon")
    chat_parser.add_argument(
        "--url",
        default=None,
        help="Daemon WebSocket URL (default: ws://127.0.0.1:8765)",
    )

    autostart_parser = subparsers.add_parser(
        "autostart",
        help="Manage Windows logon auto-start for the Clea daemon",
    )
    autostart_parser.add_argument(
        "action",
        choices=["install", "uninstall", "status"],
        help="Install, uninstall, or inspect the Task Scheduler logon task",
    )

    voice_parser = subparsers.add_parser("voice", help="Run or inspect the Clea live voice surface")
    voice_parser.add_argument(
        "--debug-audio",
        action="store_true",
        help="Print real-time audio statistics (RMS, frames/sec) and server message types",
    )
    voice_subparsers = voice_parser.add_subparsers(dest="voice_subcommand")

    voice_doctor = voice_subparsers.add_parser(
        "doctor", help="Check audio, models, and voice setup"
    )
    voice_doctor.add_argument(
        "--download-models",
        action="store_true",
        help="Download missing wake word models from official releases to <user_data_dir>/models/",
    )

    voice_test = voice_subparsers.add_parser(
        "test", help="Test audio input/output and wake word detector"
    )
    voice_test.add_argument(
        "--debug-audio",
        action="store_true",
        help="Print real-time audio statistics (RMS, frames/sec) and server message types",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for ``clea``."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "daemon":
        _run_daemon_cmd(args.host, args.port, background=args.background)
    elif args.command == "chat":
        host = os.environ.get("CLEA_HOST", DEFAULT_HOST)
        port = int(os.environ.get("CLEA_PORT", DEFAULT_PORT))
        ws_url = args.url or f"ws://{host}:{port}"
        asyncio.run(_send_and_stream(ws_url))
    elif args.command == "autostart":
        _run_autostart_cmd(args.action)
    elif args.command == "voice":
        from clea.surfaces.cli.voice import run_voice_doctor, run_voice_service, run_voice_test

        debug_audio = getattr(args, "debug_audio", False)
        if args.voice_subcommand == "doctor":
            run_voice_doctor(download_models=args.download_models)
        elif args.voice_subcommand == "test":
            run_voice_test(debug_audio=debug_audio)
        else:
            host = os.environ.get("CLEA_HOST", DEFAULT_HOST)
            port = int(os.environ.get("CLEA_PORT", DEFAULT_PORT))
            run_voice_service(host=host, port=port, debug_audio=debug_audio)
