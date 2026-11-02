import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from bisect import bisect_left
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

URL = os.environ.get("SCOREBOARD_URL", "")
INTERVAL = int(os.environ.get("INTERVAL", "60"))
DATA = Path(os.environ.get("DATA_DIR", "data"))
RAW = DATA / "raw"
HERE = Path(__file__).parent
WINDOWS = (10, 30, 60)
METRICS = ("c", "d", "l")
OUR_TEAM = os.environ.get("OUR_TEAM", "")
BASELINE_MINUTES = 60
# a gain is a spike when it is at least SPIKE_RATIO x the normal rate and SPIKE_MIN points above it
SPIKE_RATIO, SPIKE_MIN = 2, 5
ROUND_MINUTES = 10
last_error = {"msg": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS team (ts INTEGER, team TEXT, points INTEGER, uptime REAL, PRIMARY KEY (ts, team));
CREATE TABLE IF NOT EXISTS service (ts INTEGER, team TEXT, service TEXT, status TEXT,
    c INTEGER, d INTEGER, l INTEGER, PRIMARY KEY (ts, team, service));
CREATE INDEX IF NOT EXISTS service_by_cell ON service (team, service, ts);
"""


def connect(path=None):
    db = sqlite3.connect(path or DATA / "scores.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# shortcut: guessed from photos (table: # | team | points | uptime | one cell per service), not real HTML.
# Fix this function against the live page, then run `python app.py reparse` to rebuild from data/raw.
CELL = re.compile(r"(\S+)\s+C\s*(\d+)\s*D\s*(\d+)\s*L\s*(\d+)")


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    header, *rows = soup.find_all("tr")
    services = [th.get_text(strip=True) for th in header.find_all(["th", "td"])][4:]
    teams, svcs = [], []
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue
        name = next(cells[1].stripped_strings)
        teams.append((name, int(cells[2].get_text(strip=True)), float(cells[3].get_text(strip=True).rstrip("%"))))
        for svc, cell in zip(services, cells[4:]):
            if m := CELL.search(cell.get_text(" ")):
                svcs.append((name, svc, m[1], int(m[2]), int(m[3]), int(m[4])))
    return teams, svcs


def store(db, ts, html):
    teams, svcs = parse(html)
    if not teams:
        raise ValueError("parsed 0 teams")
    with db:
        db.executemany("INSERT OR REPLACE INTO team VALUES (?,?,?,?)", [(ts, *t) for t in teams])
        db.executemany("INSERT OR REPLACE INTO service VALUES (?,?,?,?,?,?,?)", [(ts, *s) for s in svcs])


def fail(msg):
    last_error["msg"] = msg
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, traceback.format_exc(), flush=True)


def scrape_loop():
    db = connect()
    while True:
        ts = int(time.time())
        try:
            body = urlopen(URL, timeout=30).read()
        except OSError:
            fail("can't connect to scoreboard")
        else:
            try:
                # raw copy first, so a parser bug never loses data
                (RAW / f"{ts}.raw").write_bytes(body)
                store(db, ts, body.decode("utf-8", "replace"))
                last_error["msg"] = None
            except Exception:
                fail("scoreboard reached, but saving or parsing failed (see docker compose logs)")
        time.sleep(INTERVAL - time.time() % INTERVAL)


def reparse():
    db = connect()
    with db:
        db.execute("DELETE FROM team")
        db.execute("DELETE FROM service")
    for f in sorted(RAW.glob("*.raw")):
        try:
            store(db, int(f.stem), f.read_text("utf-8", "replace"))
        except Exception as e:
            print(f.name, repr(e))


def window_data(db, minutes):
    last = db.execute("SELECT max(ts) FROM team").fetchone()[0]
    if last is None:
        return None, [], []
    base = (db.execute("SELECT max(ts) FROM team WHERE ts <= ?", (last - minutes * 60,)).fetchone()[0]
            or db.execute("SELECT min(ts) FROM team").fetchone()[0])
    # the hour before the window is the "normal" rate a spike is measured against
    prev = db.execute("SELECT max(ts) FROM team WHERE ts <= ?", (base - BASELINE_MINUTES * 60,)).fetchone()[0]
    scale = (last - base) / (base - prev) if prev is not None else None
    teams = db.execute("""
        SELECT n.team, n.points, n.points - coalesce(o.points, 0), coalesce(o.points, 0) - coalesce(p.points, 0),
            n.uptime
        FROM team n LEFT JOIN team o ON o.ts = ? AND o.team = n.team LEFT JOIN team p ON p.ts = ? AND p.team = n.team
        WHERE n.ts = ? ORDER BY n.points DESC""", (base, prev, last)).fetchall()
    svcs = db.execute("""
        SELECT n.team, n.service, n.status, n.c, n.d, n.l,
            n.c - coalesce(o.c, 0), n.d - coalesce(o.d, 0), n.l - coalesce(o.l, 0),
            o.c - p.c, o.d - p.d, o.l - p.l
        FROM service n
        LEFT JOIN service o ON o.ts = ? AND o.team = n.team AND o.service = n.service
        LEFT JOIN service p ON p.ts = ? AND p.team = n.team AND p.service = n.service
        WHERE n.ts = ?""", (base, prev, last)).fetchall()
    # services younger than the baseline period get no baseline, so a new spawn does not light up for everyone
    expect = lambda raw: None if scale is None or raw is None else raw * scale
    teams = [(*r[:3], expect(r[3]), r[4]) for r in teams]
    svcs = [(*r[:9], *map(expect, r[9:])) for r in svcs]
    return last, teams, svcs


def spike(gain, expected):
    if expected is None or gain - expected < SPIKE_MIN or gain < SPIKE_RATIO * expected:
        return None
    return gain / expected if expected > 0 else float("inf")


def rate(rows, span=300):
    # per-minute gain averaged over `span` seconds, because scoring ticks make per-scrape diffs jumpy
    ts = [t for t, _ in rows]
    out = []
    for t, v in rows:
        j = bisect_left(ts, t - span)
        if ts[j] < t:
            out.append((t, round((v - rows[j][1]) * 60 / (t - ts[j]), 1)))
    return out


TITLE = "CTF score tracker"
STATUS_COLOR = {"OK": "#3ddc97", "FC": "#ffb454", "FF": "#ff5c8a"}


def spike_label(ratio):
    return "new" if ratio == float("inf") else f"x{ratio:.0f}"


env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True)
env.filters.update(hm=lambda ts: time.strftime("%H:%M", time.localtime(ts)),
                   hms=lambda ts: time.strftime("%H:%M:%S", time.localtime(ts)),
                   spike=spike_label)
env.globals["status_color"] = STATUS_COLOR


def render(template, **ctx):
    return env.get_template(template).render(**ctx)


def graph_url(team, service, metric, minutes, **extra):
    return "/graph?" + urlencode({"team": team, "service": service, "metric": metric, "window": minutes, **extra})


def metric_link(team, service, metric, gain, current, minutes, expected=None):
    return {"label": "P" if metric == "points" else metric.upper(), "gain": gain, "current": current,
            "expected": expected, "ratio": spike(gain, expected), "kind": "bad" if metric == "l" else "good",
            "url": graph_url(team, service, metric, minutes)}


def index(db, minutes):
    last, teams, svcs = window_data(db, minutes)
    ctx = dict(title=TITLE, windows=WINDOWS, minutes=minutes, last=last, error=last_error["msg"], rows=[],
               now_ms=round(time.time() * 1000), round_secs=ROUND_MINUTES * 60,
               spike_ratio=SPIKE_RATIO, baseline_minutes=BASELINE_MINUTES)
    if last is None:
        return render("index.html", **ctx)
    ctx["age"] = int(time.time()) - last
    ctx["stale"] = ctx["age"] > 3 * INTERVAL
    services = [r[0] for r in db.execute(
        "SELECT service FROM service GROUP BY service ORDER BY min(ts), min(rowid)")]
    by = {(r[0], r[1]): r for r in svcs}
    hist = {}
    for team, s, st in db.execute("SELECT team, service, status FROM service WHERE ts > ? ORDER BY ts",
                                  (last - minutes * 60,)):
        hist.setdefault((team, s), []).append(st)
    rows, hot = [], []
    for team, points, dpoints, epoints, uptime in teams:
        mine = [by[team, s] for s in services if (team, s) in by]
        total = [metric_link(team, "", "points", dpoints, points, minutes, epoints)]
        total += [metric_link(team, "", m, sum(r[6 + i] for r in mine), sum(r[3 + i] for r in mine), minutes,
                              None if any(r[9 + i] is None for r in mine) else sum(r[9 + i] for r in mine))
                  for i, m in enumerate(METRICS)]
        cells = []
        for s in services:
            if (team, s) not in by:
                cells.append(None)
                continue
            _, _, st, c, d, l, gc, gd, gl, ec, ed, el = by[team, s]
            links = [metric_link(team, s, m, g, cur, minutes, e)
                     for m, g, cur, e in zip(METRICS, (gc, gd, gl), (c, d, l), (ec, ed, el))]
            hot += [dict(x, team=team, service=s, ours=team == OUR_TEAM and x["label"] == "L")
                    for x in links if x["ratio"]]
            cells.append({"status": st, "hist": hist.get((team, s), []), "metrics": links,
                          "url": graph_url(team, s, "status", minutes)})
        rows.append({"team": team, "points": points, "uptime": uptime, "ours": team == OUR_TEAM,
                     "total": total, "cells": cells})
    hot.sort(key=lambda h: (not h["ours"], -h["ratio"], -h["gain"]))
    no_baseline = teams[0][3] is None
    ctx.update(services=services, rows=rows, hot=hot[:5],
               hot_note=f"no baseline yet, needs {minutes + BASELINE_MINUTES} min of data" if no_baseline
               else "nothing unusual")
    return render("index.html", **ctx)


def chart(rows):
    if len(rows) < 2:
        return None
    w, h = 900, 300
    t0, t1 = rows[0][0], rows[-1][0]
    lo, hi = min(v for _, v in rows), max(v for _, v in rows)
    span = (hi - lo) or 1
    pts = " ".join(f"{(t - t0) / (t1 - t0) * w:.1f},{h - (v - lo) / span * h:.1f}" for t, v in rows)
    return {"w": w, "h": h, "pts": pts, "lo": lo, "hi": hi, "t0": t0, "t1": t1}


def downtime(rows):
    runs = []
    for ts, st in rows:
        if runs and runs[-1][2] == st:
            runs[-1][1] = ts
        else:
            runs.append([ts, ts, st])
    return [r for r in reversed(runs) if r[2] != "OK"]


def graph(db, q, minutes):
    team, service, metric = q.get("team", ""), q.get("service", ""), q.get("metric", "")
    if metric == "status":
        rows = db.execute("SELECT ts, status FROM service WHERE team = ? AND service = ? ORDER BY ts",
                          (team, service)).fetchall()
        return render("status.html", title=f"{team} {service} downtime", minutes=minutes, interval=INTERVAL,
                      statuses=[s for _, s in rows], down=downtime(rows),
                      first=rows[0][0] if rows else None, last=rows[-1][0] if rows else None)
    if metric == "points":
        rows = db.execute("SELECT ts, points FROM team WHERE team = ? ORDER BY ts", (team,)).fetchall()
    elif metric in METRICS and service:
        rows = db.execute(f"SELECT ts, {metric} FROM service WHERE team = ? AND service = ? ORDER BY ts",
                          (team, service)).fetchall()
    elif metric in METRICS:
        rows = db.execute(f"SELECT ts, sum({metric}) FROM service WHERE team = ? GROUP BY ts ORDER BY ts",
                          (team,)).fetchall()
    else:
        return None
    per_min = q.get("mode") == "rate"
    toggle = [{"label": label, "on": on, "url": graph_url(team, service, metric, minutes, mode=mode)}
              for mode, label, on in (("total", "running total", not per_min),
                                      ("rate", "per minute (5 min avg)", per_min))]
    return render("graph.html", title=f"{team} {service or 'total'} {metric.upper()}", minutes=minutes,
                  toggle=toggle, c=chart(rate(rows) if per_min else rows))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        minutes = int(q["window"]) if q.get("window") in [str(w) for w in WINDOWS] else WINDOWS[0]
        db = connect()
        try:
            body = index(db, minutes) if u.path == "/" else graph(db, q, minutes) if u.path == "/graph" else None
        finally:
            db.close()
        if body is None:
            return self.send_error(404)
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    RAW.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:] == ["reparse"]:
        reparse()
        sys.exit()
    threading.Thread(target=scrape_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
