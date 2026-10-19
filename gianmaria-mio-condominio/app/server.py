#!/usr/bin/env python3
"""Mio Condominio - import rate da Danea MioCondominio + API + pagina index.html.

Solo libreria standard di Python (nessuna dipendenza esterna).

Endpoint:
  GET  /             -> index.html
  GET  /api/rate     -> rate raggruppate per "tipo" + totali (JSON)
  GET  /api/status   -> ultimo import (orario, esito, log)
  POST /api/sync     -> avvia subito un import da Danea
"""

import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ======================================
# CONFIG
# ======================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("DB_PATH", "/data/Condominio.db")
PORT = int(os.environ.get("PORT", "8080"))
SYNC_EVERY_HOURS = float(os.environ.get("SYNC_EVERY_HOURS", "6"))

DB_ID = "37980"
USERNAME = "25357"
PASSWORD = "Albero042025!"

LOGIN_URL = "https://identity.cloud.danea.it/api/login/miocondominio"
API_BASE = "https://mc.cloud.danea.it/api"

# ======================================
# STATO DELL'ULTIMO IMPORT
# ======================================

sync_lock = threading.Lock()
status = {"running": False, "last_run": None, "ok": None, "log": []}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    status["log"].append(line)
    del status["log"][:-200]


# ======================================
# DATABASE
# ======================================

def get_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rate (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            num INTEGER,
            data TEXT,
            descrizione TEXT,
            dovuto REAL,
            saldo REAL,
            scaduta INTEGER,
            stato TEXT,
            tipo TEXT
        )
        """
    )
    return db


# ======================================
# CLIENT DANEA
# ======================================

def http_json(url, payload=None, token=None):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} su {url}")
    except Exception as e:  # rete, JSON non valido, ecc.
        log(f"Errore su {url}: {e}")
    return None


def as_list(x):
    """Danea a volte risponde con una lista, a volte con un oggetto che la contiene."""
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for v in x.values():
            if isinstance(v, list):
                return v
    return []


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def login():
    json_resp = http_json(
        LOGIN_URL,
        {"dbId": DB_ID, "isAdmin": False, "username": USERNAME, "password": PASSWORD},
    )
    if not json_resp or "access_token" not in json_resp:
        return None
    try:
        user_id = json_resp["profile"]["sub"].split("##")[2]
    except (KeyError, IndexError):
        user_id = None
    return {"token": json_resp["access_token"], "user_id": user_id}


# ======================================
# IMPORT DATI
# ======================================

def importa_dati(db, url, tipo, token):
    resp = http_json(url, token=token)
    if not isinstance(resp, dict) or "rate" not in resp:
        log(f"Nessuna rata in {url}")
        return 0

    oggi = date.today()
    n = 0

    for row in resp["rate"]:
        num = row.get("num")
        data_scad = str(row.get("data", ""))[:10]
        descrizione = row.get("descrizione")
        dovuto = float(row.get("dovuto") or 0)
        saldo = float(row.get("saldo") or 0)

        # salta rate inutili
        if dovuto <= 0:
            continue

        # stato locale
        giorni = (date.fromisoformat(data_scad) - oggi).days
        if saldo == 0:
            stato = "PAGATA"
        elif giorni < 0:
            stato = "SCADUTA"
        elif giorni <= 30:
            stato = "IN SCADENZA"
        else:
            stato = ""
        scaduta = 1 if giorni < 0 else 0

        existing = db.execute(
            "SELECT id, stato FROM rate WHERE num = ? AND tipo = ?", (num, tipo)
        ).fetchone()

        if existing:
            stato_esistente = (existing["stato"] or "").strip()
            # le rate pagate (PAGATA o data di pagamento) non si toccano più
            if stato_esistente == "PAGATA" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", stato_esistente):
                continue
            db.execute(
                """UPDATE rate SET data=?, descrizione=?, dovuto=?, saldo=?,
                   scaduta=?, stato=? WHERE id=?""",
                (data_scad, descrizione, dovuto, saldo, scaduta, stato, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO rate (num, data, descrizione, dovuto, saldo, scaduta, stato, tipo)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (num, data_scad, descrizione, dovuto, saldo, scaduta, stato, tipo),
            )
        n += 1

    db.commit()
    return n


def sync():
    if not sync_lock.acquire(blocking=False):
        return False  # import già in corso
    status["running"] = True
    status["log"] = []
    ok = False
    try:
        log("Login a Danea...")
        auth = login()
        if not auth or not auth["user_id"]:
            log("Login fallito")
            return False
        token, uid = auth["token"], auth["user_id"]

        db = get_db()
        totale = 0
        for cond in as_list(http_json(f"{API_BASE}/users/{uid}/condomini", token=token)):
            cid = cond["id"]
            for es in as_list(http_json(f"{API_BASE}/users/{uid}/condomini/{cid}/esercizi", token=token)):
                eid = es["id"]
                tipo = "[" + str(es["descrizione"]) + "]"
                unita = as_list(
                    http_json(f"{API_BASE}/users/{uid}/condomini/{cid}/esercizi/{eid}/unita", token=token)
                )
                ids = [str(u["unitaID"]) for u in unita if u.get("unitaID") is not None]
                if not ids:
                    continue
                url = (f"{API_BASE}/users/{uid}/payments"
                       f"?condGenID={cid}&exerciseId={eid}&unitList={','.join(ids)}")
                totale += importa_dati(db, url, tipo, token)
        db.close()
        log(f"Import completato: {totale} rate aggiornate/inserite")
        ok = True
        return True
    except Exception:
        log("Errore: " + traceback.format_exc())
        return False
    finally:
        status.update(running=False, last_run=datetime.now().isoformat(timespec="seconds"), ok=ok)
        sync_lock.release()


def scheduler():
    while True:
        sync()
        time.sleep(SYNC_EVERY_HOURS * 3600)


# ======================================
# API PER index.html
# ======================================

def rate_json():
    db = get_db()
    rows = db.execute("SELECT * FROM rate ORDER BY tipo, data, num").fetchall()
    db.close()

    oggi = date.today()
    gruppi = {}
    for r in rows:
        d = dict(r)
        stato = (d["stato"] or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stato):
            d["pagata"], d["data_pagamento"] = True, stato
            d["stato_label"] = "PAGATA"
        else:
            d["pagata"], d["data_pagamento"] = stato == "PAGATA", None
            giorni = (date.fromisoformat(d["data"]) - oggi).days
            d["stato_label"] = "PAGATA" if d["pagata"] else (
                "SCADUTA" if giorni < 0 else "IN SCADENZA" if giorni <= 30 else "DA PAGARE"
            )
        gruppi.setdefault(d["tipo"] or "", []).append(d)

    tot_da_pagare = sum(r["dovuto"] for g in gruppi.values() for r in g if not r["pagata"])
    tot_scadute = sum(r["dovuto"] for g in gruppi.values() for r in g if r["stato_label"] == "SCADUTA")
    prossima = min(
        (r["data"] for g in gruppi.values() for r in g if not r["pagata"]), default=None
    )
    return {
        "gruppi": [{"tipo": t, "rate": rs} for t, rs in gruppi.items()],
        "totali": {"da_pagare": tot_da_pagare, "scadute": tot_scadute, "prossima_scadenza": prossima},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/rate":
            self._send(200, rate_json())
        elif path == "/api/status":
            self._send(200, status)
        else:
            self._send(404, {"error": "non trovato"})

    def do_POST(self):
        if self.path.split("?")[0] == "/api/sync":
            if status["running"]:
                return self._send(409, {"error": "import già in corso"})
            threading.Thread(target=sync, daemon=True).start()
            self._send(202, {"started": True})
        else:
            self._send(404, {"error": "non trovato"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    get_db().close()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Mio Condominio in ascolto sulla porta {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
