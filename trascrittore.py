"""Trascrittore .brAIan: incolli un link YouTube, ottieni trascrizione + argomenti a colori + immagini utili.

Si apre nel browser su http://127.0.0.1:8781 (solo questo computer).
Avvio: python trascrittore.py   (oppure avvia.cmd su Windows, ./avvia.sh su macOS/Linux)
"""
import json
import os
import subprocess
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import motore

PORTA = int(os.environ.get("TRASCRITTORE_PORTA") or 8781)
QUI = Path(__file__).resolve().parent
LAVORO = {"attivo": False, "log": [], "cartella": None, "errore": None}
BLOCCO = threading.Lock()


def avvisa(msg, sostituisci=False):
    with BLOCCO:
        if sostituisci and LAVORO["log"] and LAVORO["log"][-1].get("sost"):
            LAVORO["log"][-1] = {"msg": msg, "sost": True}
        else:
            LAVORO["log"].append({"msg": msg, "sost": sostituisci})


def esegui(url, foto):
    try:
        c = motore.lavora(url, foto, avvisa)
        LAVORO["cartella"] = c.name
    except Exception as ex:
        LAVORO["errore"] = f"{type(ex).__name__}: {ex}"
        (QUI / "ultimo_errore.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        LAVORO["attivo"] = False


def archivio():
    out = []
    if motore.USCITA.exists():
        for d in sorted(motore.USCITA.iterdir(), reverse=True):
            f = d / "dati.json"
            if f.exists():
                try:
                    i = json.loads(f.read_text(encoding="utf-8"))["info"]
                    out.append({"cartella": d.name, "titolo": i["titolo"], "canale": i["canale"],
                                "immagini": len(list((d / "fotogrammi").glob("*.jpg"))) if (d / "fotogrammi").exists() else 0})
                except Exception:
                    pass
    return out[:40]


class Gestore(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _invia(self, codice, corpo, tipo="application/json; charset=utf-8"):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False)
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codice)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORTA}", f"localhost:{PORTA}")

    def do_GET(self):
        if not self._host_ok():
            return self._invia(403, {"errore": "host"})
        p = unquote(urlparse(self.path).path)
        if p == "/":
            return self._invia(200, (QUI / "pagina.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if p == "/api/stato":
            with BLOCCO:
                return self._invia(200, {**{k: LAVORO[k] for k in ("attivo", "cartella", "errore")},
                                         "log": [x["msg"] for x in LAVORO["log"]]})
        if p == "/api/archivio":
            return self._invia(200, archivio())
        if p.startswith("/out/"):
            f = (motore.USCITA / p[5:]).resolve()
            if motore.USCITA.resolve() not in f.parents or not f.is_file():
                return self._invia(404, {"errore": "non trovato"})
            tipo = {".html": "text/html; charset=utf-8", ".md": "text/markdown; charset=utf-8",
                    ".jpg": "image/jpeg", ".json": "application/json"}.get(f.suffix, "application/octet-stream")
            return self._invia(200, f.read_bytes(), tipo)
        self._invia(404, {"errore": "non trovato"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORTA}", f"http://localhost:{PORTA}"):
            return self._invia(403, {"errore": "host"})
        dati = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        p = urlparse(self.path).path
        if p == "/api/avvia":
            url = (dati.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._invia(400, {"errore": "Incolla un link che inizia con https://"})
            if LAVORO["attivo"]:
                return self._invia(409, {"errore": "Sto già lavorando a un video: aspetta che finisca."})
            LAVORO.update(attivo=True, log=[], cartella=None, errore=None)
            threading.Thread(target=esegui, args=(url, bool(dati.get("fotogrammi", True))), daemon=True).start()
            return self._invia(200, {"ok": True})
        if p == "/api/apri":
            d = (motore.USCITA / (dati.get("cartella") or "")).resolve()
            if motore.USCITA.resolve() in d.parents and d.is_dir():
                if os.name == "nt":
                    os.startfile(str(d))
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(d)])
                return self._invia(200, {"ok": True})
            return self._invia(404, {"errore": "cartella non trovata"})
        self._invia(404, {"errore": "non trovato"})


if __name__ == "__main__":
    motore.USCITA.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Gestore)
    indirizzo = f"http://127.0.0.1:{PORTA}/"
    print("Trascrittore .brAIan in ascolto su", indirizzo, "(chiudi questa finestra per spegnerlo)")
    print("Risultati in:", motore.USCITA, "· modello:", motore.MODELLO)
    if "--senza-browser" not in sys.argv:
        webbrowser.open(indirizzo)
    srv.serve_forever()
