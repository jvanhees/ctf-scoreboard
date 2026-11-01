import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from bisect import bisect_left
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from bs4 import BeautifulSoup

URL = os.environ.get("SCOREBOARD_URL", "")
INTERVAL = int(os.environ.get("INTERVAL", "60"))
DATA = Path(os.environ.get("DATA_DIR", "data"))
RAW = DATA / "raw"
WINDOWS = (10, 30, 60)
METRICS = ("c", "d", "l")
OUR_TEAM = os.environ.get("OUR_TEAM", "")
BASELINE_MINUTES = 60
# a gain is a spike when it is at least SPIKE_RATIO x the normal rate and SPIKE_MIN points above it
SPIKE_RATIO, SPIKE_MIN = 2, 5
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
CSS = """
:root{--bg:#0b1622;--panel:#122233;--line:#1f344a;--text:#e6edf3;--dim:#6b8299;--ok:#3ddc97;--fc:#ffb454;--ff:#ff5c8a}
body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px 32px}
h1{font-size:22px;margin:0 0 4px}
.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0 20px;color:var(--dim)}
.pill{padding:4px 14px;border-radius:999px;background:var(--panel);color:var(--text);text-decoration:none}
.pill.on{background:var(--ok);color:var(--bg);font-weight:600}
.scroll{overflow-x:auto}
table{border-collapse:separate;border-spacing:0;background:var(--panel);border-radius:8px}
th,td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
thead th{color:var(--dim);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
tbody th{white-space:nowrap}
tbody tr:hover{background:#16293d}
td.total{background:#0f1d2c}
.team{font-size:15px}.sub{color:var(--dim);font-size:12px;font-weight:400}
.m{display:flex;justify-content:space-between;gap:16px;color:var(--text);text-decoration:none;font-variant-numeric:tabular-nums}
.m:hover{color:var(--ok)}.k{color:var(--dim)}.m.zero span:last-child{color:var(--dim)}
.st{display:block;text-decoration:none;color:var(--dim);font-size:12px}
.st div{display:flex;justify-content:space-between;gap:8px}
.badge{font-weight:700}.OK{color:var(--ok)}.FC{color:var(--fc)}.FF,.err{color:var(--ff)}
.strip{display:block;width:100%;margin:3px 0 6px}
.strip+table,div+table{margin-top:16px}
.muted{color:var(--dim)}a{color:var(--text)}
.hot{border-radius:4px;padding:0 4px;margin:0 -4px}.hot b{font-size:11px;margin-right:4px}
.hot.good{background:rgba(61,220,151,.2)}.hot.bad{background:rgba(255,92,138,.25)}
.hotlist a{padding:2px 10px;border-radius:999px;text-decoration:none}
tr.ours>*{background:#17344d}
dialog{width:min(1000px,95vw);height:min(600px,90vh);padding:0;background:var(--bg);border:1px solid var(--line);border-radius:10px}
dialog::backdrop{background:rgba(0,0,0,.6)}dialog iframe{width:100%;height:100%;border:0}
dialog button{position:absolute;top:12px;right:12px;font-size:20px}
button{background:var(--panel);color:var(--text);border:0;border-radius:999px;padding:2px 12px;cursor:pointer}
.embed .back{display:none}
"""


# graph links open in a popup on the dashboard; reload pauses while it is open, or it would close it
JS = """
if (top !== self) {
  document.documentElement.classList.add("embed");
  addEventListener("keydown", e => { if (e.key === "Escape") parent.document.querySelector("dialog").close(); });
}
const dlg = document.querySelector("dialog");
setInterval(() => { if (!dlg || !dlg.open) location.reload(); }, 10000);
document.addEventListener("click", e => {
  const a = e.target.closest('a[href^="/graph"]');
  if (dlg && a) { e.preventDefault(); dlg.querySelector("iframe").src = a.href; dlg.showModal(); }
  else if (e.target === dlg || e.target.closest("dialog button")) dlg.close();
});
"""


def page(title, body):
    return (f"<!doctype html><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width'><title>{escape(title)}</title>"
            f"<style>{CSS}</style><h1>{escape(title)}</h1>{body}<script>{JS}</script>")


def hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def graph_url(team, service, metric, minutes, **extra):
    return "/graph?" + urlencode({"team": team, "service": service, "metric": metric, "window": minutes, **extra})


def spike_label(ratio):
    return "new" if ratio == float("inf") else f"x{ratio:.0f}"


def link(team, service, metric, gain, current, minutes, expected=None):
    label = "P" if metric == "points" else metric.upper()
    r = spike(gain, expected)
    cls = "m" + (" zero" if gain == 0 else "") + (f" hot {'bad' if metric == 'l' else 'good'}" if r else "")
    normal = "" if expected is None else f", normal ~{expected:.0f}"
    mark = f"<b>{spike_label(r)}</b>" if r else ""
    return (f'<a href="{graph_url(team, service, metric, minutes)}" class="{cls}" title="now {current}{normal}">'
            f'<span class="k">{label}</span><span>{mark}{gain:+d}</span></a>')


def strip(statuses, height=6):
    # shortcut: one slot per scrape, so periods where the scraper itself failed are not visible
    rects = "".join(f'<rect x="{i}" width="1.05" height="1" fill="{STATUS_COLOR.get(s, "#6b8299")}"/>'
                    for i, s in enumerate(statuses))
    return (f'<svg class="strip" viewBox="0 0 {len(statuses)} 1" preserveAspectRatio="none" shape-rendering="crispEdges" '
            f'style="height:{height}px">{rects}</svg>')


def index(db, minutes):
    last, teams, svcs = window_data(db, minutes)
    bar = [f'<a class="pill{" on" if w == minutes else ""}" href="/?window={w}">{w} min</a>' for w in WINDOWS]
    if last is not None:
        age = int(time.time()) - last
        bar.append(f'<span class="{"err" if age > 3 * INTERVAL else ""}">'
                   f'last scrape {time.strftime("%H:%M:%S", time.localtime(last))} ({age}s ago)</span>')
    if last_error["msg"]:
        bar.append(f'<span class="err">{escape(last_error["msg"])}</span>')
    head = '<div class="bar">' + "".join(bar) + "</div>"
    if last is None:
        return page(TITLE, head + "<p>No data yet.</p>")
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
        total = [link(team, "", "points", dpoints, points, minutes, epoints)]
        total += [link(team, "", m, sum(r[6 + i] for r in mine), sum(r[3 + i] for r in mine), minutes,
                       None if any(r[9 + i] is None for r in mine) else sum(r[9 + i] for r in mine))
                  for i, m in enumerate(METRICS)]
        cells = [f'<th><div class="team">{escape(team)}</div><div class="sub">{points} pts</div></th>',
                 f'<td class="total"><div class="st"><div><span>SLA</span>'
                 f'<span>{uptime:g}%</span></div></div>' + "".join(total) + "</td>"]
        for s in services:
            if (team, s) not in by:
                cells.append("<td></td>")
                continue
            _, _, st, c, d, l, gc, gd, gl, ec, ed, el = by[team, s]
            for m, g, e in zip(METRICS, (gc, gd, gl), (ec, ed, el)):
                if r := spike(g, e):
                    hot.append((team == OUR_TEAM and m == "l", r, g, team, s, m))
            q = urlencode({"team": team, "service": s, "metric": "status", "window": minutes})
            cells.append(f'<td><a class="st" href="/graph?{q}" title="downtime history">'
                         f'<div><span class="badge {escape(st)}">{escape(st)}</span></div>'
                         f'{strip(hist.get((team, s), []))}</a>'
                         + "".join(link(team, s, m, g, cur, minutes, e)
                                   for m, g, cur, e in zip(METRICS, (gc, gd, gl), (c, d, l), (ec, ed, el))) + "</td>")
        rows.append(f'<tr class="{"ours" if team == OUR_TEAM else ""}">' + "".join(cells) + "</tr>")
    if teams[0][3] is None:
        hotlist = f"no baseline yet, needs {minutes + BASELINE_MINUTES} min of data"
    else:
        hot.sort(key=lambda h: (not h[0], -h[1], -h[2]))
        hotlist = "".join(f'<a class="hot {"bad" if m == "l" else "good"}" href="{graph_url(t, s, m, minutes)}">'
                          f'{escape(t)} {escape(s)} {m.upper()} {g:+d} ({spike_label(r)})</a>'
                          for _, r, g, t, s, m in hot[:5]) or "nothing unusual"
    head += f'<div class="bar hotlist"><span>Hot right now:</span>{hotlist}</div>'
    table = ('<div class="scroll"><table><thead><tr><th>Team</th><th>Total</th>'
             + "".join(f"<th>{escape(s)}</th>" for s in services)
             + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    legend = (f'<p class="muted">Gains over the last {minutes} minutes. P points, C captured, D defence, L see board legend. '
              f'SLA is the uptime from the board; the bar under each status shows the status per scrape '
              f'(<span class="OK">OK</span> <span class="FC">FC</span> <span class="FF">FF</span>). '
              f'Highlighted: at least {SPIKE_RATIO}x the normal rate of the {BASELINE_MINUTES} min before (green gaining, red losing). '
              f'Click a number for its graph, a status for its downtime history. Hover a number for its current value.</p>')
    popup = '<dialog><button aria-label="close">&times;</button><iframe title="graph"></iframe></dialog>'
    return page(TITLE, head + table + legend + popup)


def svg(rows):
    if len(rows) < 2:
        return "<p>Not enough data yet.</p>"
    w, h = 900, 300
    t0, t1 = rows[0][0], rows[-1][0]
    lo, hi = min(v for _, v in rows), max(v for _, v in rows)
    span = (hi - lo) or 1
    pts = " ".join(f"{(t - t0) / (t1 - t0) * w:.1f},{h - (v - lo) / span * h:.1f}" for t, v in rows)
    txt = 'fill="#6b8299" font-size="12"'
    return (f'<svg viewBox="-50 -10 {w + 60} {h + 30}" width="100%" style="max-width:{w + 60}px">'
            f'<line x1="0" y1="{h}" x2="{w}" y2="{h}" stroke="#1f344a"/>'
            f'<polyline points="{pts}" fill="none" stroke="#3ddc97" stroke-width="2"/>'
            f'<text x="-8" y="5" text-anchor="end" {txt}>{hi:g}</text>'
            f'<text x="-8" y="{h}" text-anchor="end" {txt}>{lo:g}</text>'
            f'<text x="0" y="{h + 20}" {txt}>{hm(t0)}</text>'
            f'<text x="{w}" y="{h + 20}" text-anchor="end" {txt}>{hm(t1)}</text></svg>')


def status_history(rows):
    if not rows:
        return "<p>No data yet.</p>"
    runs = []
    for ts, st in rows:
        if runs and runs[-1][2] == st:
            runs[-1][1] = ts
        else:
            runs.append([ts, ts, st])
    down = [r for r in reversed(runs) if r[2] != "OK"]
    out = (f'<p class="muted">{hm(rows[0][0])} to {hm(rows[-1][0])}: {len(rows)} scrapes, '
           f'{len(down)} downtime periods</p><div style="max-width:910px">{strip([s for _, s in rows], 28)}</div>')
    if down:
        out += ('<table><thead><tr><th>From</th><th>To</th><th>Status</th><th>Duration</th></tr></thead><tbody>'
                + "".join(f'<tr><td>{hm(a)}</td><td>{hm(b)}</td><td class="badge {escape(st)}">{escape(st)}</td>'
                          f'<td>{(b - a + INTERVAL) / 60:g} min</td></tr>' for a, b, st in down)
                + "</tbody></table>")
    return out


def graph(db, q, minutes):
    team, service, metric = q.get("team", ""), q.get("service", ""), q.get("metric", "")
    back = f'<a class="pill back" href="/?window={minutes}">back</a>'
    if metric == "status":
        rows = db.execute("SELECT ts, status FROM service WHERE team = ? AND service = ? ORDER BY ts",
                          (team, service)).fetchall()
        return page(f"{team} {service} downtime", f'<div class="bar">{back}</div>' + status_history(rows))
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
    toggle = "".join(f'<a class="pill{" on" if on else ""}" href="{graph_url(team, service, metric, minutes, mode=mode)}">'
                    f'{label}</a>' for mode, label, on in (("total", "running total", not per_min),
                                                            ("rate", "per minute (5 min avg)", per_min)))
    return page(f"{team} {service or 'total'} {metric.upper()}", f'<div class="bar">{back}{toggle}</div>' + svg(rate(rows) if per_min else rows))


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
