"""Lifecycle helpers and CLI for the authenticated observer receiver."""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from types import TracebackType

import uvicorn

from pikvm_agent.harness.receiver import ObserverReceiver


class RunningObserverReceiver:
    """Serve the observer's write-only surface on a caller-selected address.

    The benchmark keeps direct access to ``receiver`` for exact scoring. The
    evaluator API is therefore unnecessary here and is never exposed.
    """

    def __init__(
        self,
        *,
        artifact: Path,
        token: str,
        host: str,
        port: int,
    ) -> None:
        self.receiver = ObserverReceiver(artifact=artifact, token=token)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.receiver.public_app,
                host=host,
                port=port,
                log_level="warning",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> RunningObserverReceiver:
        self.start()
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start(self, *, timeout_s: float = 10.0) -> None:
        if self.thread.is_alive():
            return
        self.thread.start()
        deadline = time.monotonic() + timeout_s
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("observer receiver exited during startup")
            if time.monotonic() >= deadline:
                self.close()
                raise RuntimeError("observer receiver did not start")
            time.sleep(0.02)

    def close(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Receive exact benchmark observations from a disposable VM."
    )
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--token", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--public-port", required=True, type=int)
    parser.add_argument("--evaluator-port", required=True, type=int)
    args = parser.parse_args()

    receiver = ObserverReceiver(artifact=args.artifact, token=args.token)
    public_server = uvicorn.Server(
        uvicorn.Config(
            receiver.public_app,
            host=args.host,
            port=args.public_port,
            log_level="info",
        )
    )
    public_thread = threading.Thread(target=public_server.run, daemon=True)
    public_thread.start()
    try:
        # The evaluator is intentionally fixed to loopback and must never be
        # passed to the public tunnel.
        uvicorn.run(
            receiver.evaluator_app,
            host="127.0.0.1",
            port=args.evaluator_port,
            log_level="info",
        )
    finally:
        public_server.should_exit = True
        public_thread.join(timeout=5)


if __name__ == "__main__":
    main()
