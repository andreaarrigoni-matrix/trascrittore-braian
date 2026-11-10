"""Motore del Trascrittore .brAIan.

YouTube -> trascrizione parola per parola -> argomenti per importanza (modello locale) -> fotogrammi utili
-> un file .md per le AI e un .html colorato da leggere.

Tutto gira sul tuo computer: i sottotitoli e il video arrivano da YouTube, l'analisi la fa un modello locale
con Ollama (testo + immagini), la trascrizione di riserva la fa Whisper. Niente va a servizi di AI esterni.

Impostazioni (variabili d'ambiente, tutte facoltative):
  TRASCRITTORE_USCITA   cartella dove salvare i risultati   (predefinita: ~/Trascrizioni)
  TRASCRITTORE_MODELLO  modello Ollama con visione          (predefinito: gemma4:12b)
  OLLAMA_HOST           indirizzo di Ollama                 (predefinito: http://127.0.0.1:11434)
"""
import base64
import html
import io
import json
import os
import re
import site
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

USCITA = Path(os.environ.get("TRASCRITTORE_USCITA") or Path.home() / "Trascrizioni").expanduser()
def _indirizzo_ollama(v: str) -> str:
    # OLLAMA_HOST spesso vale "0.0.0.0" (dove Ollama ascolta): per collegarsi serve 127.0.0.1
    v = (v or "127.0.0.1").strip().rstrip("/")
    if "://" not in v:
        v = "http://" + v
    schema, resto = v.split("://", 1)
    host, _, porta = resto.partition(":")
    if host in ("", "0.0.0.0", "[::]", "::"):
        host = "127.0.0.1"
    return f"{schema}://{host}:{porta or '11434'}"


OLLAMA = _indirizzo_ollama(os.environ.get("OLLAMA_HOST", ""))
MODELLO = os.environ.get("TRASCRITTORE_MODELLO") or "gemma4:12b"   # serve un modello che veda anche le immagini
LINGUE = ["it", "en", "es", "fr", "de", "nl", "pt"]
BLOCCO_SECONDI = 40
MAX_CANDIDATI = 48                 # fotogrammi da far guardare al modello al massimo
COLORE = {"alta": "🔴", "media": "🟡", "bassa": "🟢"}
NOME_LIV = {"alta": "fondamentale", "media": "importante", "bassa": "contesto"}


# ---------------------------------------------------------------- utilità
def mmss(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def nome_cartella(titolo: str) -> str:
    t = re.sub(r"[^\w\s-]", "", titolo, flags=re.UNICODE).strip()
    t = re.sub(r"\s+", "_", t)[:70] or "video"
    return f"{datetime.now():%Y-%m-%d}_{t}"


def link_t(url: str, s: float) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(s)}s"


def ollama(messaggi, formato_json=True, timeout=900, max_token=None):
    # Niente "format: json" di Ollama: con Gemma 4 a volte torna vuoto. Gemma scrive il JSON da sola
    # (spesso dentro ```json … ```) e qui lo estraiamo.
    opzioni = {"temperature": 0.2}
    if max_token:
        opzioni["num_predict"] = max_token
    corpo = {"model": MODELLO, "messages": messaggi, "stream": False, "think": False,
             "keep_alive": "30m", "options": opzioni}
    req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(corpo).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        testo = json.load(r)["message"]["content"]
    if not formato_json:
        return testo
    testo = re.sub(r"^\s*```(?:json)?|```\s*$", "", testo.strip(), flags=re.M)
    try:
        return json.loads(testo)
    except ValueError:
        m = re.search(r"\{.*\}", testo, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            return {}


# ---------------------------------------------------------------- 1. informazioni
def informazioni(url: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True}) as y:
        i = y.extract_info(url, download=False)
    data = i.get("upload_date") or ""
    return {
        "id": i["id"], "titolo": i.get("title") or i["id"], "canale": i.get("channel") or i.get("uploader") or "",
        "url": i.get("webpage_url") or url, "durata": i.get("duration") or 0,
        "data": f"{data[:4]}-{data[4:6]}-{data[6:]}" if len(data) == 8 else "",
        "lingua": (i.get("language") or "")[:2], "capitoli": i.get("chapters") or [],
        "descrizione": (i.get("description") or "")[:1500],
    }


# ---------------------------------------------------------------- 2. trascrizione
def sottotitoli(info: dict):
    from youtube_transcript_api import YouTubeTranscriptApi
    lingue = ([info["lingua"]] if info["lingua"] else []) + [l for l in LINGUE if l != info["lingua"]]
    elenco = YouTubeTranscriptApi().list(info["id"])
    scelto, tipo = None, ""
    for cerca, t in ((elenco.find_manually_created_transcript, "sottotitoli dell'autore"),
                     (elenco.find_generated_transcript, "sottotitoli automatici di YouTube")):
        try:
            scelto, tipo = cerca(lingue), t
            break
        except Exception:
            pass
    if scelto is None:
        scelto = next(iter(elenco))
        tipo = "sottotitoli automatici di YouTube" if scelto.is_generated else "sottotitoli dell'autore"
    righe = scelto.fetch().to_raw_data()
    return [{"inizio": r["start"], "fine": r["start"] + r.get("duration", 0), "testo": r["text"]} for r in righe], \
        f"{tipo} ({scelto.language_code})"


def _cuda_dll():
    if os.name != "nt":
        return
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        for sotto in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
            p = Path(base) / sotto
            if p.is_dir():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")


def whisper(info: dict, cartella_tmp: Path, avvisa):
    import yt_dlp
    avvisa("Niente sottotitoli: scarico l'audio e lo trascrivo con Whisper")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "format": "bestaudio/best",
                           "outtmpl": str(cartella_tmp / "audio.%(ext)s")}) as y:
        y.download([info["url"]])
    audio = next(cartella_tmp.glob("audio.*"))
    from faster_whisper import WhisperModel
    _cuda_dll()
    try:
        modello = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        motore = "Whisper large-v3-turbo (GPU)"
    except Exception:
        modello = WhisperModel("small", device="cpu", compute_type="int8")
        motore = "Whisper small (CPU)"
    segmenti, meta = modello.transcribe(str(audio), vad_filter=True)
    out = []
    for s in segmenti:
        out.append({"inizio": s.start, "fine": s.end, "testo": s.text.strip()})
        if info["durata"]:
            avvisa(f"Trascrivo… {mmss(s.end)} di {mmss(info['durata'])}", sostituisci=True)
    return out, f"{motore}, lingua {meta.language}"


def a_blocchi(righe, capitoli):
    tagli = sorted(c["start_time"] for c in capitoli if c.get("start_time"))
    blocchi, cur = [], None
    for r in righe:
        testo = re.sub(r"\s+", " ", r["testo"].replace("\n", " ")).strip()
        if not testo:
            continue
        nuovo_cap = cur and any(cur["inizio"] < t <= r["inizio"] for t in tagli)
        if cur is None or r["inizio"] - cur["inizio"] >= BLOCCO_SECONDI or nuovo_cap:
            cur = {"id": len(blocchi) + 1, "inizio": r["inizio"], "fine": r["fine"], "testo": testo}
            blocchi.append(cur)
        else:
            cur["testo"] += " " + testo
            cur["fine"] = r["fine"]
    return blocchi


# ---------------------------------------------------------------- 3. analisi con il modello locale
ISTR = """Sei un analista attento. Ricevi la trascrizione di un video divisa in blocchi numerati [B<n> mm:ss].
Rispondi SOLO con JSON valido, in italiano, con questa forma:
{"riassunto": "4-6 frasi su cosa dice il video e perché conta",
 "argomenti": [{"titolo": "titolo breve", "importanza": "alta|media|bassa", "sintesi": "2-3 frasi", "blocchi": [n, ...]}],
 "livelli": {"alta": [n, ...], "media": [n, ...]}}
Regole: da 4 a 10 argomenti, ordinati per importanza. "alta" = il cuore del video (concetti, istruzioni, dati, decisioni).
"media" = supporto utile. "bassa" = contesto, saluti, pubblicità. In "livelli" metti i blocchi davvero più densi:
"alta" al massimo il 20% dei blocchi, "media" al massimo il 30%. Non inventare: usa solo quello che c'è nel testo."""


def analizza(info, blocchi, avvisa):
    righe = [f"[B{b['id']} {mmss(b['inizio'])}] {b['testo']}" for b in blocchi]
    finestre, cur, n = [], [], 0
    for r in righe:                       # finestre di ~60k caratteri (circa 15k token)
        if n + len(r) > 60000 and cur:
            finestre.append(cur)
            cur, n = [], 0
        cur.append(r)
        n += len(r)
    if cur:
        finestre.append(cur)
    risultati = []
    for i, f in enumerate(finestre, 1):
        avvisa(f"L'AI legge e pesa gli argomenti ({i}/{len(finestre)})…", sostituisci=i > 1)
        testa = f"Video: {info['titolo']} — {info['canale']}\n"
        if info["capitoli"]:
            testa += "Capitoli: " + "; ".join(f"{mmss(c['start_time'])} {c['title']}" for c in info["capitoli"]) + "\n"
        risultati.append(ollama([{"role": "system", "content": ISTR},
                                 {"role": "user", "content": testa + "\n" + "\n".join(f)}]))
    if len(risultati) == 1:
        a = risultati[0]
    else:
        a = {"riassunto": " ".join(r.get("riassunto", "") for r in risultati),
             "argomenti": [x for r in risultati for x in r.get("argomenti", [])],
             "livelli": {k: [b for r in risultati for b in (r.get("livelli") or {}).get(k, [])] for k in ("alta", "media")}}
        avvisa("L'AI unisce le parti in un riassunto solo…")
        try:
            a["riassunto"] = ollama([{"role": "user", "content": "Unisci in un unico riassunto italiano di 5-7 frasi, "
                                      "rispondi SOLO con JSON {\"riassunto\": \"...\"}:\n" + a["riassunto"]}]).get("riassunto", a["riassunto"])
        except Exception:
            pass
    ordine = {"alta": 0, "media": 1, "bassa": 2}
    validi = {b["id"] for b in blocchi}
    argomenti = []
    for x in a.get("argomenti") or []:
        imp = str(x.get("importanza", "media")).lower()
        imp = imp if imp in ordine else "media"
        ids = sorted({int(str(n).lstrip("B")) for n in x.get("blocchi") or [] if str(n).lstrip("B").isdigit()} & validi)
        argomenti.append({"titolo": str(x.get("titolo", "")).strip() or "Senza titolo", "importanza": imp,
                          "sintesi": str(x.get("sintesi", "")).strip(), "blocchi": ids})
    argomenti.sort(key=lambda x: (ordine[x["importanza"]], x["blocchi"][0] if x["blocchi"] else 0))
    livello = {}
    for k in ("media", "alta"):
        for n in (a.get("livelli") or {}).get(k, []):
            try:
                n = int(str(n).lstrip("B"))
            except ValueError:
                continue
            if n in validi:
                livello[n] = k
    return {"riassunto": str(a.get("riassunto", "")).strip(), "argomenti": argomenti, "livello": livello}


# ---------------------------------------------------------------- 4. fotogrammi
VISTA = """Guarda questo fotogramma di un video.{contesto}
Rispondi SOLO con JSON:
{{"persona_grande": true|false, "valore": 0-5,
 "tipo": "diagramma|schema|slide|grafico|tabella|codice|interfaccia|mappa|prodotto|foto|persona|altro",
 "descrizione": "una riga in italiano su cosa mostra e quale informazione contiene"}}
"persona_grande" = true se una persona (viso o busto) occupa più di un quarto dell'immagine.
"valore" = quanto questa immagine aiuta a capire il video SENZA guardarlo:
 5 = diagramma, schema, tabella, grafico o slide che spiega un concetto;
 4 = schermata di software o codice che mostra un passaggio preciso o un risultato, grande e leggibile;
 2 = pagina generica (home di un sito, registrazione, login, chat vuota, cartelle vuote);
 0-1 = persona che parla o reagisce, loghi, copertina, b-roll, schermate nere o di transizione.
Il piccolo riquadro della persona in un angolo non conta: giudica il contenuto.
La descrizione deve essere una sola riga, al massimo 25 parole: non copiare il testo che vedi."""
VALORE_MINIMO = 4      # sotto questo voto l'immagine si scarta
MAX_IMMAGINI = 12      # in tutto, le migliori
SIMILE = 20           # differenza media (0-255) sotto la quale due immagini tenute sono "la stessa schermata"


def fotogrammi(info, cartella: Path, cartella_tmp: Path, avvisa, argomenti=()):
    import av
    import yt_dlp
    from PIL import Image, ImageChops, ImageStat
    avvisa("Scarico il video (max 720p) per cercare diagrammi e immagini…")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                           "format": "bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]/best[height<=720]/best",
                           "outtmpl": str(cartella_tmp / "video.%(ext)s")}) as y:
        y.download([info["url"]])
    video = next(cartella_tmp.glob("video.*"))
    candidati, ultima, ultimo_t = [], None, -99
    with av.open(str(video)) as c:
        s = c.streams.video[0]
        s.codec_context.skip_frame = "NONKEY"          # solo i fotogrammi chiave: veloce
        for f in c.decode(s):
            t = float(f.pts * s.time_base) if f.pts is not None else 0
            img = f.to_image()
            mini = img.convert("L").resize((64, 36))
            if ultima is None or (ImageStat.Stat(ImageChops.difference(mini, ultima)).mean[0] > 14 and t - ultimo_t > 3):
                candidati.append((t, img))
                ultima, ultimo_t = mini, t
    if len(candidati) > MAX_CANDIDATI:
        passo = len(candidati) / MAX_CANDIDATI
        candidati = [candidati[int(i * passo)] for i in range(MAX_CANDIDATI)]
    avvisa(f"{len(candidati)} cambi di scena: l'AI li guarda uno per uno…")
    (cartella / "fotogrammi").mkdir(exist_ok=True)
    for vecchio in (cartella / "fotogrammi").glob("*.jpg"):     # stesso video rifatto: si riparte puliti
        vecchio.unlink()
    contesto = ""
    if argomenti:
        contesto = f"\nVideo: «{info['titolo']}». Argomenti: " + "; ".join(argomenti[:10]) + "."
    prompt = VISTA.format(contesto=contesto)
    buoni = []
    for k, (t, img) in enumerate(candidati, 1):
        avvisa(f"L'AI guarda il fotogramma {k}/{len(candidati)} ({mmss(t)})", sostituisci=True)
        anteprima = img.copy()
        anteprima.thumbnail((768, 768))
        buf = io.BytesIO()
        anteprima.save(buf, "JPEG", quality=85)
        try:
            g = ollama([{"role": "user", "content": prompt, "images": [base64.b64encode(buf.getvalue()).decode()]}],
                       timeout=300, max_token=180)
        except Exception:
            continue
        try:
            voto = int(float(g.get("valore", 0)))
        except (TypeError, ValueError):
            voto = 0
        if g.get("persona_grande") is True or voto < VALORE_MINIMO:
            continue
        desc = re.sub(r"\s+", " ", str(g.get("descrizione", ""))).strip()
        impronta = img.convert("L").resize((48, 27))
        buoni.append({"t": t, "img": img, "impronta": impronta, "voto": voto,
                      "tipo": str(g.get("tipo", "")), "descrizione": desc[:220]})
    # le migliori prima; una schermata quasi uguale a una già tenuta si scarta
    tenuti = []
    for b in sorted(buoni, key=lambda b: (-b["voto"], b["t"])):
        if any(ImageStat.Stat(ImageChops.difference(b["impronta"], x["impronta"])).mean[0] < SIMILE for x in tenuti):
            continue
        tenuti.append(b)
        if len(tenuti) >= MAX_IMMAGINI:
            break
    scelti = []
    for b in sorted(tenuti, key=lambda b: b["t"]):
        nome = f"fotogrammi/{mmss(b['t']).replace(':', '-')}.jpg"
        b["img"].save(cartella / nome, "JPEG", quality=88)
        scelti.append({"t": b["t"], "file": nome, "tipo": b["tipo"], "descrizione": b["descrizione"], "valore": b["voto"]})
    avvisa(f"L'AI ne ha promosse {len(buoni)}, tengo le {len(scelti)} migliori e diverse tra loro")
    return scelti


# ---------------------------------------------------------------- 5. scrittura
def scrivi(info, origine, blocchi, an, foto, cartella: Path):
    def blocco_di(t):
        for b in blocchi:
            if b["inizio"] <= t <= b["fine"] + 2:
                return b["id"]
        return min(blocchi, key=lambda b: abs(b["inizio"] - t))["id"] if blocchi else None

    for f in foto:
        f["blocco"] = blocco_di(f["t"])
        f["argomenti"] = [a["titolo"] for a in an["argomenti"] if f["blocco"] in a["blocchi"]]
    per_id = {b["id"]: b for b in blocchi}
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---- Markdown per le AI
    md = ["---", f"titolo: \"{info['titolo']}\"", f"canale: \"{info['canale']}\"", f"url: {info['url']}",
          f"data_video: {info['data']}", f"durata: {mmss(info['durata'])}", f"trascrizione: {origine}",
          f"analisi: {MODELLO} in locale (Ollama)",f"creato: {adesso}", "---", "",
          f"# {info['titolo']}", "", f"**{info['canale']}** · {info['data']} · {mmss(info['durata'])} · [guarda su YouTube]({info['url']})", "",
          "> **Legenda importanza:** 🔴 fondamentale · 🟡 importante · 🟢 contesto. "
          "Il testo della trascrizione è parola per parola: i colori indicano solo il peso dei passaggi.", "",
          "## Riassunto", "", an["riassunto"] or "_(non disponibile)_", "", "## Argomenti per importanza", ""]
    for a in an["argomenti"]:
        md += [f"### {COLORE[a['importanza']]} {a['titolo']} — {NOME_LIV[a['importanza']]}", "", a["sintesi"], ""]
        if a["blocchi"]:
            md.append("Passaggi: " + " · ".join(f"[{mmss(per_id[n]['inizio'])}]({link_t(info['url'], per_id[n]['inizio'])})"
                                                for n in a["blocchi"] if n in per_id))
            md.append("")
        for f in foto:
            if a["titolo"] in f["argomenti"]:
                md += [f"![{f['descrizione']}]({f['file']})", f"*{mmss(f['t'])} · {f['tipo']} · {f['descrizione']}*", ""]
    if foto:
        md += ["## Immagini e diagrammi", ""]
        for f in foto:
            md += [f"- **{mmss(f['t'])}** ({f['tipo']}) {f['descrizione']} → `{f['file']}`"
                   + (f" · argomento: {', '.join(f['argomenti'])}" if f["argomenti"] else "")]
        md.append("")
    md += ["## Trascrizione completa", ""]
    for b in blocchi:
        liv = an["livello"].get(b["id"])
        segno = COLORE.get(liv, "")
        testo = f"**{b['testo']}**" if liv == "alta" else b["testo"]
        md += [f"[{mmss(b['inizio'])}]({link_t(info['url'], b['inizio'])}) {segno} {testo}".replace("  ", " "), ""]
    (cartella / "trascrizione.md").write_text("\n".join(md), encoding="utf-8")

    # ---- HTML colorato (stile plancia + cervello)
    e = html.escape
    card = []
    for a in an["argomenti"]:
        passaggi = " ".join(f"<a href='#b{n}'>{mmss(per_id[n]['inizio'])}</a>" for n in a["blocchi"] if n in per_id)
        imm = "".join(f"<figure><img src='{e(f['file'])}' loading='lazy'><figcaption>{mmss(f['t'])} · {e(f['descrizione'])}</figcaption></figure>"
                      for f in foto if a["titolo"] in f["argomenti"])
        card.append(f"<article class='card {a['importanza']}'><div class='liv'>{COLORE[a['importanza']]} {NOME_LIV[a['importanza']]}</div>"
                    f"<h3>{e(a['titolo'])}</h3><p>{e(a['sintesi'])}</p><div class='pass'>{passaggi}</div>{imm}</article>")
    trascr = []
    for b in blocchi:
        liv = an["livello"].get(b["id"], "")
        trascr.append(f"<p id='b{b['id']}' class='blk {liv}'><a class='t' href='{e(link_t(info['url'], b['inizio']))}' target='_blank'>"
                      f"{mmss(b['inizio'])}</a> {e(b['testo'])}</p>")
    galleria = "".join(f"<figure><a href='#b{f['blocco']}'><img src='{e(f['file'])}' loading='lazy'></a>"
                       f"<figcaption><b>{mmss(f['t'])}</b> · {e(f['tipo'])} · {e(f['descrizione'])}</figcaption></figure>" for f in foto)
    pagina = f"""<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(info['titolo'])}</title><style>
:root{{--bg:#07090c;--pan:#10141b;--tx:#e8edf2;--mu:#9aa6b2;--co:#ff8a65;--cy:#4dd9e8;--am:#ffc857;--ro:#ff5d5d;--gi:#ffd23f;--ve:#5ad28c}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:16px/1.6 "Segoe UI",system-ui,sans-serif}}
main{{max-width:900px;margin:auto;padding:28px 18px 80px}}
.eti{{color:var(--co);letter-spacing:.12em;font-size:13px;text-transform:uppercase}}
h1{{font-size:34px;line-height:1.15;margin:10px 0 8px}}.meta{{color:var(--mu)}}.meta a{{color:var(--cy)}}
nav{{margin:18px 0;padding:12px 0;border-top:1px solid #1d2430;border-bottom:1px solid #1d2430}}nav a{{color:var(--cy);margin-right:16px}}
.leg span{{margin-right:14px;color:var(--mu);font-size:14px}}
.riass{{background:linear-gradient(180deg,#141a22,#0e1218);border:1px solid #233041;border-radius:12px;padding:18px 20px;margin:18px 0}}
.riass h2{{margin:0 0 8px;font-size:20px;color:var(--cy)}}
h2.sez{{font-size:22px;margin:34px 0 12px}}
.card{{background:var(--pan);border-left:4px solid var(--mu);border-radius:0 10px 10px 0;padding:14px 18px;margin:12px 0}}
.card.alta{{border-color:var(--ro)}}.card.media{{border-color:var(--gi)}}.card.bassa{{border-color:var(--ve)}}
.card h3{{margin:2px 0 6px;font-size:21px}}.card p{{margin:0 0 8px;color:#cfd7df}}.liv{{font-size:12px;color:var(--mu);text-transform:uppercase;letter-spacing:.08em}}
.pass a{{color:var(--cy);margin-right:10px;font-size:14px}}
figure{{margin:12px 0}}img{{max-width:100%;border-radius:8px;border:1px solid #233041}}
.card figure{{display:inline-block;vertical-align:top;width:260px;margin:8px 10px 0 0}}.card figure img{{width:260px;height:146px;object-fit:cover;cursor:zoom-in}}figcaption{{color:var(--mu);font-size:13px}}
.gal{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
.blk{{padding:6px 10px;margin:4px 0;border-radius:6px}}.blk.alta{{background:rgba(255,93,93,.14);border-left:3px solid var(--ro);font-weight:500}}
.blk.media{{background:rgba(255,210,63,.10);border-left:3px solid var(--gi)}}.t{{color:var(--am);font-family:Consolas,monospace;font-size:13px;margin-right:6px;text-decoration:none}}
:target{{outline:2px solid var(--cy)}}
</style></head><body><main>
<div class="eti">Trascrittore .brAIan · {e(info['data'])} · {mmss(info['durata'])}</div>
<h1>{e(info['titolo'])}</h1>
<div class="meta">{e(info['canale'])} · <a href="{e(info['url'])}" target="_blank">guarda su YouTube</a> · {e(origine)}</div>
<nav><a href="#riassunto">Riassunto</a><a href="#argomenti">Argomenti</a>{'<a href="#immagini">Immagini</a>' if foto else ''}<a href="#trascrizione">Trascrizione</a></nav>
<div class="leg"><span>🔴 fondamentale</span><span>🟡 importante</span><span>🟢 contesto</span></div>
<section class="riass" id="riassunto"><h2>Riassunto</h2><p>{e(an['riassunto'])}</p></section>
<h2 class="sez" id="argomenti">Argomenti per importanza</h2>{''.join(card)}
{f'<h2 class="sez" id="immagini">Immagini e diagrammi</h2><div class="gal">{galleria}</div>' if foto else ''}
<h2 class="sez" id="trascrizione">Trascrizione completa</h2>{''.join(trascr)}
</main></body></html>"""
    (cartella / "trascrizione.html").write_text(pagina, encoding="utf-8")
    (cartella / "dati.json").write_text(json.dumps({"info": info, "origine": origine, "blocchi": blocchi, "analisi": an,
                                                    "fotogrammi": foto}, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- tutto insieme
def lavora(url: str, con_fotogrammi: bool, avvisa) -> Path:
    avvisa("Leggo le informazioni del video…")
    info = informazioni(url)
    cartella = USCITA / nome_cartella(info["titolo"])
    cartella.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        avvisa(f"«{info['titolo']}» · {info['canale']} · {mmss(info['durata'])}")
        try:
            avvisa("Cerco i sottotitoli su YouTube…")
            righe, origine = sottotitoli(info)
        except Exception:
            righe, origine = whisper(info, tmp, avvisa)
        blocchi = a_blocchi(righe, info["capitoli"])
        avvisa(f"Trascrizione pronta: {len(blocchi)} blocchi, fonte: {origine}")
        an = analizza(info, blocchi, avvisa)
        foto = []
        if con_fotogrammi:
            try:
                foto = fotogrammi(info, cartella, tmp, avvisa, [a["titolo"] for a in an["argomenti"]])
                avvisa(f"Immagini utili trovate: {len(foto)}")
            except Exception as ex:
                avvisa(f"Fotogrammi saltati: {ex}")
        scrivi(info, origine, blocchi, an, foto, cartella)
    avvisa("Fatto.")
    return cartella


if __name__ == "__main__":       # uso da riga di comando: python motore.py URL [--senza-immagini]
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or not sys.argv[1].startswith("http"):
        sys.exit("Uso: python motore.py https://youtu.be/... [--senza-immagini]")
    c = lavora(sys.argv[1], "--senza-immagini" not in sys.argv, lambda m, sostituisci=False: print(m, flush=True))
    print(c)
