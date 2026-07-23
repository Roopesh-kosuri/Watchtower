"""
Deliberately-flaky local HTTP server, used ONLY for Phase 1 testing.

This container's egress is allowlisted to a fixed set of domains, so a real
public endpoint can't be used to reliably produce failures/timeouts within a
short test window. This server stands in for "a real URL that intermittently
fails/timeouts" by injecting failures on a known distribution:
  - ~35% of requests hang for 8s (the test client times out at 3s)
  - ~20% of requests return HTTP 500
  - ~45% of requests succeed normally with a 200 + JSON body
"""

import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FlakyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        roll = random.random()
        if roll < 0.35:
            time.sleep(8)  # will exceed the 3s client timeout used in testing
            # fall through and respond anyway once woken, in case the client
            # is patient -- but by then the client has already given up
        if roll < 0.55:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal Server Error")
            return
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence default per-request stderr logging


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), FlakyHandler)
    server.daemon_threads = True
    server.serve_forever()
