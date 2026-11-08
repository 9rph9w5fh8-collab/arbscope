#!/usr/bin/env python3
"""Polymarket-Scanner fuer a-Shell auf dem iPhone. Nur lesen.

Liest die oeffentlichen Orderbuecher der meistgehandelten Polymarket-Maerkte,
zeigt sie als Tabelle und speichert jede Runde in pmscan_log.csv.
Kein Login, keine Keys, keine Orders. Nur Python-Standardbibliothek.

Start:      python3 pmscan.py
Laenger:    python3 pmscan.py --minutes 30
Auswerten:  python3 pmscan.py --summary
Hilfe:      python3 pmscan.py --help

Daten: Gamma API (Marktliste) und CLOB API (Orderbuecher), siehe
https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body.md
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LOG = "pmscan_log.csv"
BATCH = 50
WIDTH = 46  # passt hochkant auf ein iPhone in a-Shell

# Nur diese zwei Lese-Aufrufe sind erlaubt. POST /books liest Orderbuecher, es ist keine Order.
ALLOWED = {("GET", "/markets"), ("POST", "/books")}

COLUMNS = ["ts_utc", "slug", "question", "outcome", "token_id", "best_bid", "best_ask",
           "mid", "spread", "bid_depth_usd", "ask_depth_usd", "set_cost", "set_gap", "set_max_size"]


# ---------------- HTTP (nur lesen) ----------------

def curl_works() -> bool:
    """In a-Shell haengt Pythons urllib, das eingebaute curl funktioniert."""
    out = os.path.join(tempfile.gettempdir(), "pmscan_curltest.txt")
    try:
        rc = os.system(f'curl --version > "{out}"')
        with open(out, encoding="utf-8", errors="replace") as fh:
            ok = rc == 0 and "curl" in fh.read().lower()
        os.remove(out)
        return ok
    except OSError:
        return False


class Api:
    def __init__(self, gamma: str = GAMMA, clob: str = CLOB, timeout: float = 10.0, use_curl: bool = False) -> None:
        self.gamma, self.clob, self.timeout = gamma.rstrip("/"), clob.rstrip("/"), timeout
        self.use_curl = use_curl
        socket.setdefaulttimeout(timeout)  # auch DNS/Handshake duerfen nicht ewig haengen

    def _curl(self, method: str, url: str, body: object) -> object:
        tmp = tempfile.gettempdir()
        out, inp = os.path.join(tmp, "pmscan_resp.json"), os.path.join(tmp, "pmscan_req.json")
        if os.path.exists(out):
            os.remove(out)
        cmd = f'curl -s -f -m {int(self.timeout)} -A "pmscan-readonly/1.0" -H "Accept: application/json"'
        if body is not None:
            with open(inp, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
            cmd += f' -X POST -H "Content-Type: application/json" --data-binary "@{inp}"'
        rc = os.system(f'{cmd} -o "{out}" "{url}"')
        if rc != 0 or not os.path.exists(out):
            raise OSError(f"curl Fehlercode {rc}")
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)

    def _call(self, method: str, url: str, body: object = None) -> object:
        if (method, urlsplit(url).path) not in ALLOWED:
            raise PermissionError(f"{method} {url} ist kein erlaubter Lese-Aufruf")
        if self.use_curl:
            last = None
            for attempt in range(3):
                try:
                    return self._curl(method, url, body)
                except (OSError, ValueError) as e:
                    last = str(e)
                time.sleep(min(2 ** attempt, 8))
            raise ConnectionError(f"{method} {urlsplit(url).netloc}{urlsplit(url).path}: {last}")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"User-Agent": "pmscan-readonly/1.0", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = str(getattr(e, "reason", e))
            time.sleep(min(2 ** attempt, 8))
        raise ConnectionError(f"{method} {urlsplit(url).netloc}{urlsplit(url).path}: {last}")

    def markets(self, limit: int) -> list[dict]:
        q = urlencode({"active": "true", "closed": "false", "limit": max(limit * 2, 20),
                       "order": "volume24hr", "ascending": "false"})
        raw = self._call("GET", f"{self.gamma}/markets?{q}")
        out = [m for m in (parse_market(r) for r in (raw or [])) if m]
        out.sort(key=lambda m: m["volume24hr"], reverse=True)
        return out[:limit]

    def books(self, token_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(token_ids), BATCH):
            chunk = token_ids[i:i + BATCH]
            raw = self._call("POST", f"{self.clob}/books", [{"token_id": t} for t in chunk])
            for item in raw or []:
                b = parse_book(item)
                out[b["token_id"]] = b
        return out


# ---------------- Parsen ----------------

def _as_list(v: object) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_market(r: dict) -> dict | None:
    if r.get("closed") or r.get("active") is False:
        return None
    if r.get("enableOrderBook") is False or r.get("acceptingOrders") is False:
        return None
    ids = _as_list(r.get("clobTokenIds")) or _as_list(r.get("positionIds"))
    outs = _as_list(r.get("outcomes"))
    if len(ids) < 2 or len(ids) != len(outs):
        return None
    return {"slug": str(r.get("slug") or r.get("id") or ""), "question": str(r.get("question") or ""),
            "outcomes": [str(o) for o in outs], "token_ids": [str(t) for t in ids],
            "volume24hr": _f(r.get("volume24hr"))}


def _levels(raw: object, descending: bool) -> list[tuple[float, float]]:
    out = []
    for lv in raw or []:
        p, s = _f(lv.get("price"), -1), _f(lv.get("size"))
        if 0 <= p <= 1 and s > 0:
            out.append((p, s))
    out.sort(key=lambda x: x[0], reverse=descending)
    return out


def parse_book(r: dict) -> dict:
    return {"token_id": str(r.get("asset_id", "")),
            "bids": _levels(r.get("bids"), True), "asks": _levels(r.get("asks"), False)}


# ---------------- Kennzahlen ----------------

def depth(levels: list[tuple[float, float]], n: int = 5) -> float:
    return round(sum(p * s for p, s in levels[:n]), 4)


def book_stats(b: dict) -> dict:
    bid = b["bids"][0][0] if b["bids"] else None
    ask = b["asks"][0][0] if b["asks"] else None
    both = bid is not None and ask is not None
    return {"best_bid": bid, "best_ask": ask,
            "mid": round((bid + ask) / 2, 6) if both else None,
            "spread": round(ask - bid, 6) if both else None,
            "bid_depth_usd": depth(b["bids"]), "ask_depth_usd": depth(b["asks"])}


def set_gap(books: list[dict], fee: float = 0.0) -> dict:
    """Alle Ausgaenge zum besten Ask kaufen. Genau einer zahlt 1 $.
    Positive Luecke = theoretischer Gewinn pro Set nach Gebuehr."""
    if not books or any(not b["asks"] for b in books):
        return {"set_cost": None, "set_gap": None, "set_max_size": 0.0}
    cost = sum(b["asks"][0][0] for b in books)
    return {"set_cost": round(cost, 6), "set_gap": round(1 - cost * (1 + fee), 6),
            "set_max_size": min(b["asks"][0][1] for b in books)}


# ---------------- Scan ----------------

def scan_once(api: Api, n: int, fee: float) -> list[dict]:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mkts = api.markets(n)
    books = api.books([t for m in mkts for t in m["token_ids"]])
    rows = []
    for m in mkts:
        bs = [books.get(t) for t in m["token_ids"]]
        if any(b is None for b in bs):
            continue
        g = set_gap(bs, fee)
        for outcome, b in zip(m["outcomes"], bs):
            rows.append({"ts_utc": ts, "slug": m["slug"], "question": m["question"], "outcome": outcome,
                         "token_id": b["token_id"], **book_stats(b), **g})
    return rows


def append_csv(path: str, rows: list[dict]) -> None:
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r.get(k) for k in COLUMNS})


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "~"


def table(rows: list[dict], top: int) -> str:
    """Pro Markt eine Zeile, sortiert nach Luecke. Max. WIDTH Zeichen breit."""
    per: dict[str, dict] = {}
    for r in rows:
        m = per.setdefault(r["slug"], {"slug": r["slug"], "gap": r["set_gap"], "cost": r["set_cost"],
                                       "size": r["set_max_size"], "spr": []})
        if r["spread"] is not None:
            m["spr"].append(r["spread"])
    ms = sorted(per.values(), key=lambda m: -1e9 if m["gap"] is None else m["gap"], reverse=True)
    out = ["Markt                  Summe  Lücke  Spr", "-" * WIDTH]
    for m in ms[:top]:
        cost = "  –  " if m["cost"] is None else f"{m['cost']:.3f}"
        gap = "   –  " if m["gap"] is None else f"{m['gap']:+.3f}"
        spr = "–" if not m["spr"] else f"{max(m['spr']):.2f}"
        flag = "*" if (m["gap"] or 0) > 0 else " "
        out.append(f"{flag}{_cut(m['slug'], 21):<21} {cost:>6} {gap:>6} {spr:>4}")
    return "\n".join(out)


def summarize(path: str) -> str:
    if not os.path.exists(path):
        return f"Noch keine Datei {path}. Erst einen Scan starten."
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return "Datei ist leer."
    snaps: dict[tuple[str, str], float] = {}
    for r in rows:
        if r["set_gap"]:
            snaps[(r["ts_utc"], r["slug"])] = float(r["set_gap"])
    spreads = sorted(float(r["spread"]) for r in rows if r["spread"])
    pos = {k: v for k, v in snaps.items() if v > 0}
    lines = [
        f"Zeilen: {len(rows)}",
        f"Scans: {len({r['ts_utc'] for r in rows})}",
        f"Markt-Momente: {len(snaps)}",
        f"Median-Spread: {spreads[len(spreads) // 2]:.3f} $" if spreads else "Median-Spread: –",
        f"Mit Lücke > 0: {len(pos)} ({100 * len(pos) / max(len(snaps), 1):.2f} %)",
    ]
    for (ts, slug), g in sorted(pos.items(), key=lambda kv: -kv[1])[:10]:
        try:
            hhmm = datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
        except ValueError:
            hhmm = ts[11:19]
        lines.append(f"  {hhmm} {_cut(slug, 22)} {g:+.3f}")
    if not pos:
        lines.append("Keine Lücke gefunden: Die Märkte waren")
        lines.append("in dieser Zeit fair bepreist.")
    return "\n".join(lines)


def run(api: Api, minutes: float, interval: float, n: int, fee: float, top: int, clear: bool, out: str) -> int:
    end = time.monotonic() + minutes * 60
    total, scans, hits, errors = 0, 0, 0, 0
    while time.monotonic() < end:
        t0 = time.monotonic()
        if scans == 0:
            print(f"Scan läuft, Verbindung zu Polymarket ... (Versuch {errors + 1})")
        try:
            rows = scan_once(api, n, fee)
            append_csv(out, rows)
            scans += 1
            total += len(rows)
            hits += len({r["slug"] for r in rows if (r["set_gap"] or 0) > 0})
            left = max(0, int(end - time.monotonic()))
            head = (f"Polymarket-Scanner (nur lesen)\n"
                    f"{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')}  Scan {scans}  noch {left // 60}:{left % 60:02d}\n"
                    f"Zeilen gesamt {total}  Lücken bisher {hits}\n")
            if clear:
                sys.stdout.write("\033[2J\033[H")
            print(head + table(rows, top))
            print("* = Lücke > 0. Stopp: Ctrl+C")
        except (ConnectionError, PermissionError) as e:
            errors += 1
            print(f"{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')} Fehler: {e}")
            if errors >= 5 and scans == 0:
                print("Polymarket ist nicht erreichbar. Internet prüfen")
                print("(WLAN/Mobilfunk wechseln) und neu starten.")
                return 1
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))
    print(f"\nFertig: {total} Zeilen in {out}\n")
    print(summarize(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pmscan", description="Polymarket-Scanner, nur lesen")
    p.add_argument("--minutes", type=float, default=5, help="Laufzeit (Standard 5)")
    p.add_argument("--interval", type=float, default=15, help="Sekunden zwischen Scans")
    p.add_argument("--markets", type=int, default=25, help="Anzahl Märkte nach Volumen")
    p.add_argument("--fee", type=float, default=0.0, help="Gebühr als Anteil, z. B. 0.02")
    p.add_argument("--top", type=int, default=12, help="Zeilen in der Tabelle")
    p.add_argument("--out", default=LOG, help="CSV-Datei")
    p.add_argument("--summary", action="store_true", help="nur vorhandene CSV auswerten")
    p.add_argument("--no-clear", action="store_true", help="Bildschirm nicht leeren")
    p.add_argument("--http", choices=["auto", "curl", "python"], default="curl",
                   help="Netzwerkweg (Standard curl, in a-Shell der verlaessliche Weg)")
    p.add_argument("--gamma", default=GAMMA, help=argparse.SUPPRESS)
    p.add_argument("--clob", default=CLOB, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    try:  # a-Shell puffert sonst die Ausgabe, und der Bildschirm bleibt leer
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    if a.summary:
        print(summarize(a.out))
        return 0
    try:
        print("Polymarket-Scanner startet (nur lesen).")
        use_curl = a.http == "curl" or (a.http == "auto" and curl_works())
        print("Verbindung über " + ("curl" if use_curl else "Python"))
        return run(Api(a.gamma, a.clob, use_curl=use_curl), a.minutes, a.interval, a.markets, a.fee, a.top, not a.no_clear, a.out)
    except KeyboardInterrupt:
        print("\nGestoppt.\n")
        print(summarize(a.out))
        return 0


if __name__ == "__main__":
    sys.exit(main())
