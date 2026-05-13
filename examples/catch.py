"""Tiny local webhook receiver for klaxon demos.

Stdlib only — no dependencies. Run it in one terminal:

    python examples/catch.py            # listens on http://127.0.0.1:8765/

Then in another terminal:

    socmon demo --watch --catch --interval-seconds 5

`--catch` (no value = default to http://127.0.0.1:8765/) tells `socmon demo`
to bypass whatever alerters your YAML configures and route every finding to
this URL instead — so you can show "alerts firing in real-time" in a
screen-share without pointing at a real Slack workspace.

Output format mirrors the CLI finding-line style so the catch pane reads
just like a klaxon scan summary.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer


# ANSI severity colors; degrade to no-color if the terminal doesn't support it.
_SEV_COLOR = {
    "critical": "\033[1;31m",   # bold red
    "high":     "\033[0;33m",   # orange
    "medium":   "\033[0;33m",   # yellow
    "low":      "\033[0;90m",   # gray
}
_RESET = "\033[0m"
_DIM = "\033[2m"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            f = json.loads(body)
        except json.JSONDecodeError:
            print(f"{_DIM}[?] non-JSON body ({len(body)} bytes){_RESET}", flush=True)
            self._ack()
            return

        sev = (f.get("severity") or "?").lower()
        color = _SEV_COLOR.get(sev, "")
        ts = datetime.now().strftime("%H:%M:%S")
        title = f.get("title", "(no title)")
        score = f.get("score")
        score_str = f" (score {score:.1f})" if isinstance(score, (int, float)) else ""

        sig = self.headers.get("X-Socmon-Signature", "")
        sig_str = f"  {_DIM}sig={sig[:24]}…{_RESET}" if sig else ""

        evidence_url = ""
        ev = f.get("evidence") or []
        if ev and ev[0].get("url"):
            evidence_url = f"  {_DIM}{ev[0]['url']}{_RESET}"

        line = (
            f"[{ts}] {color}[{sev.upper()}]{_RESET} "
            f"{title}{score_str}{evidence_url}{sig_str}"
        )
        print(line, flush=True)
        self._ack()

    def do_GET(self) -> None:
        # Friendly response if someone opens the URL in a browser.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write("klaxon catch - POST a Finding JSON here.\n".encode("utf-8"))

    def _ack(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_args, **_kwargs) -> None:
        # Built-in handler logs every request to stderr; we print our own
        # one-line summary instead.
        return


def main() -> None:
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"usage: python {sys.argv[0]} [PORT]", file=sys.stderr)
            sys.exit(2)
    print(
        f"klaxon catch · listening on http://127.0.0.1:{port}/  "
        f"(Ctrl-C to stop)\n",
        flush=True,
    )
    try:
        HTTPServer(("127.0.0.1", port), _Handler).serve_forever()
    except KeyboardInterrupt:
        print("\ncatch: stopped.")


if __name__ == "__main__":
    main()
