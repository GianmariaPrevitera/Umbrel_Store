#!/usr/bin/env python3
"""URL Follower - backend (versione di prova).

  /                     -> pagina web di benvenuto (www/index.html)
  /api=<9 caratteri>    -> testo semplice: "prova"
  qualsiasi altro caso  -> pagina web vuota, sempre con risposta 200 OK

Usa solo la libreria standard di Python: nessuna dipendenza da installare.
"""
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT = int(os.environ.get("PORT", "8080"))
WWW_DIR = os.environ.get("WWW_DIR", "/www")

# Chiave api: esattamente 9 caratteri tra lettere (a-z, A-Z) e numeri (0-9).
KEY_PATH = re.compile(r"/api=[A-Za-z0-9]{9}")
TEXT_REPLY = "prova"
BLANK_PAGE = (
    '<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">'
    "<title></title></head><body></body></html>"
)

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def read_welcome_page():
    try:
        with open(os.path.join(WWW_DIR, "index.html"), "rb") as handle:
            return handle.read()
    except OSError as exc:
        print("index.html non leggibile: %s" % exc, flush=True)
        return BLANK_PAGE.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "URLFollower/0.1"

    def respond(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            body, ctype = read_welcome_page(), "text/html; charset=utf-8"
        elif KEY_PATH.fullmatch(path):
            body, ctype = TEXT_REPLY.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            body, ctype = BLANK_PAGE.encode("utf-8"), "text/html; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = respond
    do_HEAD = respond


if __name__ == "__main__":
    print("URL Follower in ascolto sulla porta %d (www: %s)" % (PORT, WWW_DIR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
