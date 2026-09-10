#!/usr/bin/env python3
"""
Serves the catalog viewer.

A page cannot `fetch` a file off disk -- browsers refuse cross-origin reads on
`file://`, so opening tools/viewer/index.html directly gets you a viewer that can see
no catalog at all. This is the two-line HTTP server that fixes that, rooted at the
repository so the viewer's relative paths reach `catalog/`.

Nothing is cached. The catalog is usually being rebuilt while the viewer is open --
that is most of what it is for -- so a stale 304 would show you the bug you just fixed.

Usage:
    python tools/serve.py            # localhost:8765, opens a browser
    python tools/serve.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import http.server
import socketserver
import threading
import webbrowser
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWER = "/tools/viewer/index.html"


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        # The whole point is watching a directory change under you.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args) -> None:
        # One line per card image would drown the one line that matters.
        if "GET /catalog/" in (fmt % args) or "GET /tools/" in (fmt % args):
            return
        super().log_message(fmt, *args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if not (ROOT / "catalog").is_dir():
        print(f"No catalog/ under {ROOT}. Run pull_catalog.py first -- "
              "the viewer will load, but every set will read as missing.")

    handler = partial(Handler, directory=str(ROOT))
    # Otherwise a restart inside the TIME_WAIT window fails on an address still held by
    # the socket that just closed, which for a tool you restart constantly is most of them.
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}{VIEWER}"
        print(f"Catalog viewer:  {url}")
        print("Ctrl-C to stop.")
        if not args.no_open:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
