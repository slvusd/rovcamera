#!/usr/bin/env python3
"""
Stats proxy for Pi 4 — forwards port 9001 to ROV Pi 5 stats at :9000.
Run via: ./install.sh pi4   (installs as rov-stats-proxy.service)
"""
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

ROV_STATS_HOST = "192.168.3.52"
ROV_STATS_PORT = 9000
PROXY_PORT     = 9001
_origin        = f"http://{ROV_STATS_HOST}:{ROV_STATS_PORT}"

class Proxy(BaseHTTPRequestHandler):
    def _forward(self, method, body=None):
        req = urllib.request.Request(
            _origin + self.path,
            data=body,
            method=method,
        )
        for h in ("Content-Type", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type",   r.headers.get("Content-Type", "text/html"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.URLError as e:
            msg = f"ROV stats unreachable ({ROV_STATS_HOST}:{ROV_STATS_PORT}): {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self._forward("POST", self.rfile.read(n) if n else None)

    def log_message(self, fmt, *args):
        pass   # suppress per-request noise

if __name__ == "__main__":
    print(f"Stats proxy  :{PROXY_PORT}  →  {_origin}", flush=True)
    HTTPServer(("0.0.0.0", PROXY_PORT), Proxy).serve_forever()
