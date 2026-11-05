"""Draws the README's diagrams as SVG, in the console's palette and fonts (embedded, so GitHub's <img> shows them).
Run: python3 docs/images/diagrams.py  — writes flow.svg, journey.svg and day.svg next to this file."""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "..", "..", "ui", "fonts")
PAPER, INK, MUTED, RAIL, WELL = "#EEF1EC", "#1C2B36", "#5E6B73", "#B9C2BD", "#DCE2DD"
ANTH, VALAR, OTHER, SHADOW = "#D8901E", "#2F6DB5", "#1F8A5B", "#6B5BD6"


def font(family, weight, file):
    b = base64.b64encode(open(os.path.join(FONTS, file), "rb").read()).decode()
    return f'@font-face{{font-family:"{family}";font-weight:{weight};src:url(data:font/woff2;base64,{b}) format("woff2")}}'


STYLE = "".join([
    font("Barlow Condensed", 700, "barlow-condensed-latin-700-normal.woff2"),
    font("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal.woff2"),
    font("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal.woff2"),
    font("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2"),
]) + f"""
text{{font-family:"IBM Plex Sans",system-ui,sans-serif;fill:{INK}}}
.h{{font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:23px}}
.k{{font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:13px;letter-spacing:2.2px;fill:{MUTED}}}
.s{{font-size:12.5px;fill:{MUTED}}}
.b{{font-weight:600;font-size:14px}}
.m{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;fill:{MUTED}}}
.w{{fill:#fff}}
.t{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:14px}}
"""


def svg(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">'
            f'<title>{title}</title><style>{STYLE}</style>'
            f'<rect width="{w}" height="{h}" rx="18" fill="{PAPER}"/>{body}</svg>')


def stop(x, y, c, r=7):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff" stroke="{c}" stroke-width="4"/>'


def term(x, y, c, r=13):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}"/><circle cx="{x}" cy="{y}" r="{r - 7}" fill="#fff"/>'


def t(x, y, s, cls="", anchor="start", extra=""):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}" {extra}>{s}</text>'


def line(d, c, w=10, extra=""):
    return f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" {extra}/>'


# 1. Where requests go -------------------------------------------------------------------------------------------
def flow():
    b = [t(40, 46, "WHERE REQUESTS GO", "k")]
    y = 190
    b.append(line(f"M100,{y} L330,{y}", INK, 12))
    b.append(term(100, y, INK, 16))
    b += [t(100, y - 34, "Claude Code", "h", "middle"), t(100, y + 42, "every session", "s", "middle"),
          t(100, y + 58, "on this machine", "s", "middle")]
    b.append(t(222, y - 14, "ANTHROPIC_BASE_URL", "m", "middle"))
    branches = [(ANTH, y - 100, "Anthropic", "api.anthropic.com", [(700, "your login,"), (700, "byte for byte")]),
                (VALAR, y, "Valar", "Now · Priority · Standard", [(650, "swap key"), (790, "rename model")]),
                (OTHER, y + 100, "Any downstream", "Anthropic-compatible, up to 6", [(650, "swap key"), (790, "rename model")])]
    for i, (c, ty, name, sub, stops) in enumerate(branches):
        sy = y + (i - 1) * 13
        b.append(line(f"M470,{sy} C560,{sy} 560,{ty} 650,{ty} L900,{ty}", c, 10))
    for c, ty, name, sub, stops in branches:
        if c == ANTH:
            b += [stop(730, ty, c), t(730, ty + 26, "your login, untouched", "s", "middle"),
                  t(730, ty + 42, "byte for byte", "s", "middle")]
        else:
            for x, lab in stops:
                b += [stop(x, ty, c), t(x, ty + 26, lab, "s", "middle")]
        b += [term(912, ty, c), t(938, ty + 2, name, "h"), t(938, ty + 20, sub, "m")]
    b.append(line(f"M405,{y + 34} L405,{y + 120}", MUTED, 3, 'stroke-dasharray="2 7"'))
    b += [stop(405, y + 130, MUTED, 7), t(424, y + 127, "Console  /ui", "b"),
          t(424, y + 145, "traffic · cost · rules", "s")]
    b += [f'<rect x="330" y="{y - 34}" width="150" height="68" rx="34" fill="{INK}"/>',
          t(405, y + 2, "signaller", "h w", "middle", 'style="fill:#fff"'),
          t(405, y + 20, "127.0.0.1:8787", "m", "middle", f'style="fill:{RAIL}"')]
    return svg(1200, 360, "".join(b), "Claude Code sends every request to signaller, which forwards it to Anthropic untouched or to a downstream with its own key and model name")


# 2. One request, station by station -----------------------------------------------------------------------------
def journey():
    b = [t(40, 46, "ONE REQUEST, STATION BY STATION", "k")]
    Y, YA, YV = 130, 250, 310
    xs = {"lever": 210, 1: 360, 2: 490, 3: 620, 4: 750, "o": 880}
    # rails
    b.append(line(f"M{xs['lever']},{YA} L1010,{YA}", ANTH, 10))
    b.append(line(f"M{xs['lever']},{YV} L1010,{YV}", VALAR, 10))
    # spurs: (x, rail, label)
    spurs = [(xs["lever"] - 8, YA, None), (xs["lever"] + 8, YV, None),
             (xs[1], YA, "server tools"),
             (xs[2] - 8, YA, "#planning"), (xs[2] + 8, YV, "#execution"),
             (xs[3] - 8, YA, "plan · review"), (xs[3] + 8, YV, "code · chat"),
             (xs[4] - 8, YA, "opus · fable"), (xs[4] + 8, YV, "sonnet · haiku"),
             (xs["o"], YA, None)]
    for x, ry, lab in spurs:
        c = ANTH if ry == YA else VALAR
        if ry == YV:  # crossing the amber rail: a paper gap first
            b.append(f'<rect x="{x - 5}" y="{YA - 9}" width="10" height="18" fill="{PAPER}"/>')
        dash = 'stroke-dasharray="1 7"' if x in (xs["lever"] - 8, xs["lever"] + 8) else ""
        b.append(line(f"M{x},{Y + 12} L{x},{ry}", c, 4, dash))
        b.append(f'<circle cx="{x}" cy="{ry}" r="5" fill="#fff" stroke="{c}" stroke-width="3"/>')
    def chip(cx, cy, lab, c):  # a label on a white chip, laid over the spurs
        w = len(lab) * 7 + 18
        return (f'<rect x="{cx - w / 2}" y="{cy - 12}" width="{w}" height="22" rx="11" fill="#fff" stroke="{c}" stroke-width="1.5"/>'
                + t(cx, cy + 3.5, lab, "m", "middle", f'style="fill:{c}"'))
    for x, ry, lab in spurs:
        if lab:
            k = round((x - 8 if ry == YV else x + 8) if x not in (xs[1],) else x)
            b.append(chip(k, Y + (42 if ry == YA else 74), lab, ANTH if ry == YA else VALAR))
    # main line + stations
    b.append(line(f"M80,{Y} L{xs['o']},{Y}", INK, 12))
    b += [term(80, Y, INK, 16), t(80, Y - 32, "POST", "h", "middle"), t(80, Y + 40, "/v1/messages", "m", "middle")]
    b += [f'<rect x="{xs["lever"] - 26}" y="{Y - 20}" width="52" height="40" rx="10" fill="#fff" stroke="{INK}" stroke-width="4"/>',
          t(xs["lever"], Y + 6, "⇄", "b", "middle", 'style="font-size:18px"'),
          t(xs["lever"], Y - 34, "Lever", "h", "middle")]
    b.append(chip(xs["lever"], Y + 58, "all to one track", INK))
    names = {1: "Tools", 2: "Tags", 3: "Classifier", 4: "Model tier", "o": "Otherwise"}
    for k, n in names.items():
        b += [stop(xs[k], Y, INK, 9), t(xs[k], Y - 26, n, "b", "middle")]
    b.append(f'<path d="M{xs[1] - 30},{Y - 58} L{xs[1] - 30},{Y - 66} L{xs["o"] + 40},{Y - 66} L{xs["o"] + 40},{Y - 58}" fill="none" stroke="{RAIL}" stroke-width="2"/>')
    b.append(t((xs[1] + xs["o"]) / 2, Y - 76, "RULEBOOK · TOP TO BOTTOM · FIRST THAT DECIDES WINS", "k", "middle"))
    # prepare + ends
    px = 960
    b += [f'<rect x="{px - 20}" y="{YA - 22}" width="40" height="{YV - YA + 44}" rx="20" fill="#fff" stroke="{INK}" stroke-width="4"/>',
          t(px, YV + 50, "prepare()", "b", "middle"),
          t(px, YV + 68, "rename · swap key · settings lines", "m", "middle")]
    b += [term(1030, YA, ANTH), t(1054, YA + 6, "Anthropic", "h"),
          term(1030, YV, VALAR), t(1054, YV + 6, "Valar", "h")]
    # shadow
    b.append(line(f"M{px - 120},{YV} C{px - 120},{YV + 60} {px - 160},{YV + 70} {px - 220},{YV + 70} L{px - 520},{YV + 70}",
                  SHADOW, 4, 'stroke-dasharray="6 7"'))
    b += [stop(px - 520, YV + 70, SHADOW, 6), t(px - 535, YV + 66, "doubling: a shadow copy", "b", "end"),
          t(px - 535, YV + 84, "stored for Side by side, never shown to Claude Code", "s", "end")]
    return svg(1180, 430, "".join(b), "A request passes the lever, then the rulebook's rules in order; the first that decides picks Anthropic or Valar, and prepare() readies it")


# 3. A day on Valar ----------------------------------------------------------------------------------------------
def day():
    X0, X1 = 270, 1100
    hx = lambda h: X0 + (X1 - X0) * h / 24
    b = [t(40, 46, "A DAY ON VALAR · WHICH COMPLETION WINDOW", "k")]
    for h in range(0, 25, 3):
        b += [f'<line x1="{hx(h)}" y1="78" x2="{hx(h)}" y2="236" stroke="{WELL}" stroke-width="1.5"/>',
              t(hx(h), 72, f"{h:02d}:00", "m", "middle")]
    rows = [(118, "Every session", "Tracks default + line 2", [(0, 7, "Priority", VALAR, True), (7, 24, "Now", VALAR, False)]),
            (194, "nightly infra cleanup", "line 1 · session is …", [(0, 24, "Priority all day", VALAR, True)])]
    for y, name, sub, segs in rows:
        b += [t(40, y - 2, name, "b"), t(40, y + 16, sub, "m")]
        for a, z, lab, c, solid in segs:
            x, w = hx(a) + 2, hx(z) - hx(a) - 4
            if solid:
                b.append(f'<rect x="{x}" y="{y - 20}" width="{w}" height="40" rx="20" fill="{c}"/>')
                b.append(t(x + w / 2, y + 5, lab, "b", "middle", 'style="fill:#fff"'))
            else:
                b.append(f'<rect x="{x}" y="{y - 20}" width="{w}" height="40" rx="20" fill="#fff" stroke="{c}" stroke-width="3"/>')
                b.append(t(x + w / 2, y + 5, lab, "b", "middle", f'style="fill:{c}"'))
    n = hx(33 / 60 + 1)
    b += [f'<line x1="{n}" y1="86" x2="{n}" y2="244" stroke="{INK}" stroke-width="2.5"/>',
          f'<rect x="{n - 72}" y="244" width="144" height="24" rx="12" fill="{INK}"/>',
          t(n, 260, "router time 01:33", "m", "middle", 'style="fill:#fff"')]
    b.append(t(X1, 262, "Priority costs 75% of Now · first matching line wins · the hour is pinned per turn", "s", "end"))
    return svg(1140, 290, "".join(b), "Over a day, every session uses Valar's Priority window from 00:00 to 07:00 and Now after; one picked session stays on Priority all day")


# 4. Steering from inside Claude Code ----------------------------------------------------------------------------
def steer():
    W, H, TW = 1180, 470, 720
    b = [f'<rect x="28" y="28" width="{TW}" height="{H - 56}" rx="14" fill="{INK}"/>']
    b += [f'<circle cx="{52 + i * 20}" cy="52" r="6" fill="{c}"/>' for i, c in enumerate(("#E0645A", "#E3B341", "#5BB672"))]
    b.append(t(28 + TW / 2, 57, "claude · ~/Development/infra", "m", "middle", f'style="fill:{MUTED}"'))
    G, OUT, YOU, TOOL = "#8FA0AA", "#E6ECE8", "#FFFFFF", "#B7A8F5"
    rows = [  # (text, colour, indent, gap before)
        ("> /signaller valar", YOU, 0, 0), ("signaller: valar (this session)", "#7FB2EA", 1, 0),
        ("> /signaller valar window priority", YOU, 0, 14), ("signaller: valar window priority for this session", "#7FB2EA", 1, 0),
        ("> /signaller name nightly infra cleanup", YOU, 0, 14), ('signaller: this session is "nightly infra cleanup" in the console', OUT, 1, 0),
        ("> rename the modules, then do /signaller anthropic for the review", YOU, 0, 14),
        ("⏺ signaller_lever(lever: \"anthropic\")", TOOL, 1, 0), ("signaller: anthropic (this session)", "#F0B45A", 1, 0),
    ]
    y = 100
    for text, c, ind, gap in rows:
        y += gap
        txt = text.replace("&", "&amp;").replace("<", "&lt;")
        if text.startswith("> "):
            b.append(f'<text x="52" y="{y}" class="t" style="fill:{G}">&gt;</text>')
            b.append(f'<text x="70" y="{y}" class="t" style="fill:{c};font-weight:600">{txt[2:]}</text>')
        else:
            b.append(f'<text x="{52 + ind * 18}" y="{y}" class="t" style="fill:{c}">{"⎿  " if ind and not text.startswith("⏺") else ""}{txt}</text>')
        y += 26
    # status line
    sy = H - 70
    b += [f'<line x1="48" y1="{sy - 22}" x2="{TW + 8}" y2="{sy - 22}" stroke="#34444F" stroke-width="1.5"/>',
          f'<text x="52" y="{sy + 4}" class="t" style="fill:{G}">status</text>',
          f'<rect x="118" y="{sy - 14}" width="296" height="26" rx="13" fill="#2A3A45"/>',
          f'<circle cx="136" cy="{sy - 1}" r="5" fill="{ANTH}"/>',
          f'<text x="150" y="{sy + 4}" class="t" style="fill:{OUT}">signaller: anthropic (this session)</text>']
    # callouts
    cx = TW + 70
    notes = [(116, VALAR, "Per session, live", ["Switch this session's lever with one", "slash command. Other sessions keep", "the global lever; subagents follow."]),
             (232, OTHER, "Window and model, from the prompt", ["Saved as a settings line at the top,", "so it beats schedules. Survives a", "restart; pruned after a week unseen."]),
             (348, SHADOW, "Claude can steer — only when told", ["Each /signaller you type allows one", "switch, to exactly that mode.", "Never on Claude's own judgment."])]
    anchors = [113, 191, 337]
    for (ny, c, head, lines), ay in zip(notes, anchors):
        b.append(line(f"M{TW + 14},{ay} C{TW + 40},{ay} {cx - 40},{ny - 5} {cx - 14},{ny - 5}", c, 2.5))
        b.append(f'<circle cx="{TW + 14}" cy="{ay}" r="4" fill="{c}"/>')
        b.append(f'<circle cx="{cx - 6}" cy="{ny - 5}" r="6" fill="{c}"/>')
        b.append(t(cx + 8, ny, head, "b"))
        for i, ln in enumerate(lines):
            b.append(t(cx + 8, ny + 20 + i * 17, ln, "s"))
    return svg(W, H, "".join(b), "Inside Claude Code, /signaller switches this session to Valar, sets its Valar window to Priority, names it, and Claude switches to Anthropic when the person asked for it; the status line shows router: anthropic (this session)")


for name, fn in (("flow", flow), ("journey", journey), ("day", day), ("steer", steer)):
    open(os.path.join(HERE, name + ".svg"), "w").write(fn())
print("wrote flow.svg journey.svg day.svg steer.svg")
