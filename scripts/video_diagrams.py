#!/usr/bin/env python3
"""Original motion diagrams for the research explainer production pipeline.

All geometry is illustrative. Labels and attribution come from the reviewed
script; toy comparisons are explicitly disclosed and never report paper data.
The renderer supplies a palette and a safe drawing box for each aspect ratio.
"""

from __future__ import annotations

from functools import lru_cache
import math
import os
from pathlib import Path
import re
from urllib.parse import urlparse

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Discovery and collection do not require video extras.
    Image = ImageDraw = ImageFont = None


ROOT = Path(__file__).resolve().parent.parent
VISUALS = frozenset({"neuron", "analogy", "comparison", "test", "takeaway", "source"})


def _clamp(value: float, lower: float = 0., upper: float = 1.) -> float:
    return min(upper, max(lower, value))


def _ease(value: float) -> float:
    value = _clamp(value)
    return value * value * (3 - 2 * value)


def _phase(progress: float, start: float, stop: float) -> float:
    return _ease((progress - start) / max(.001, stop - start))


def _blend(a: str, b: str, t: float) -> tuple[int,int,int]:
    def rgb(value):
        if isinstance(value,tuple):
            return value[:3]
        value=value.removeprefix("#")
        if len(value)==3:
            value="".join(v*2 for v in value)
        return tuple(int(value[i:i+2],16) for i in (0,2,4))
    left,right=rgb(a),rgb(b)
    return tuple(round(u+(v-u)*_clamp(t)) for u,v in zip(left,right))


def comparison_mode(scene: dict) -> str:
    """Select a schematic, never infer empirical values from the narration."""
    text = " ".join([str(scene.get("heading", ""))] + [str(v) for v in scene.get("labels", [])]).lower()
    if "probabil" in text:
        return "probability"
    if any(term in text for term in ("response", "strong", "activation")):
        return "activation"
    return "generic"


def analogy_mode(scene: dict) -> str:
    text = " ".join([str(scene.get("heading", "")), str(scene.get("narration", ""))] +
                    [str(v) for v in scene.get("labels", [])]).lower()
    return ("traffic_light" if re.search(
        r"\btraffic[\s-]*lights?\b|\bintersection\b|\bred\b.{0,16}\bstopped\s+cars\b", text)
            else "generic")


def toy_profiles(progress: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Normalized illustrative distributions, with no claimed measurements."""
    change = .16 * _phase(progress, .28, .80)
    return (.25, .60, .15), (.25 + change, .60 - change, .15)


@lru_cache(maxsize=256)
def _font(size: int, bold: bool = False):
    if ImageFont is None:
        raise RuntimeError("Video diagrams require Pillow; install requirements-video.txt")
    name = "Manrope-Bold.ttf" if bold else "Manrope-Regular.ttf"
    env_name = "ALIGNMENT_VIDEO_FONT_BOLD" if bold else "ALIGNMENT_VIDEO_FONT"
    candidates = [os.environ.get(env_name, ""), str(ROOT / "assets" / "fonts" / name),
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
                  "/System/Library/Fonts/Supplemental/Arial.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size=max(8, round(size)))
    return ImageFont.truetype("DejaVuSans.ttf", size=max(8, round(size)))


def _wrap(text: str, face, width: float) -> list[str]:
    lines, current = [], ""
    for word in str(text).split():
        if current and face.getlength(current + " " + word) > width:
            lines.append(current)
            current = ""
        # A URL or an unusually long technical token must still respect the box.
        while face.getlength(word) > width and len(word) > 1:
            cut = 1
            while cut < len(word) and face.getlength(word[:cut + 1]) <= width:
                cut += 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut])
            word = word[cut:]
        current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


def _text(draw, value, box, size, color, *, bold=False, align="left", lines=3):
    x, y, width, height = box
    size = max(8, round(size))
    lower = max(9, round(size * .64))
    while True:
        face = _font(size, bold)
        parts = _wrap(str(value), face, max(1, width))
        if (len(parts) <= lines and len(parts) * size * 1.24 <= height) or size <= lower:
            break
        size -= 1
    if len(parts) > lines:
        parts = parts[:lines]
        while parts[-1] and face.getlength(parts[-1] + "…") > width:
            parts[-1] = parts[-1][:-1]
        parts[-1] += "…"
    for n, part in enumerate(parts):
        tx = x + (width - face.getlength(part)) / 2 if align == "center" else x
        # Anchor top makes the measured box independent of a font's ascent.
        draw.text((round(tx), round(y + n * size * 1.24)), part, font=face, fill=color, anchor="lt")


def _line(draw, points, fill, width=2, progress=1., arrow=False):
    if progress <= 0:
        return
    a, b = points
    t = _clamp(progress)
    end = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    width = max(1, round(width))
    draw.line((a, end), fill=fill, width=width)
    if arrow and t > .98:
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        length = max(6, width * 3)
        draw.polygon([end,
                      (end[0] - math.cos(angle - .48) * length, end[1] - math.sin(angle - .48) * length),
                      (end[0] - math.cos(angle + .48) * length, end[1] - math.sin(angle + .48) * length)], fill=fill)


def _dash(draw, a, b, color, width=2, dash=8):
    length = math.dist(a, b)
    if not length:
        return
    for offset in range(0, math.ceil(length), max(2, round(dash * 1.7))):
        t, u = offset / length, min(1., (offset + dash) / length)
        draw.line(((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t),
                   (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)),
                  fill=color, width=max(1, round(width)))


def _circle(draw, center, radius, fill=None, outline=None, width=2):
    x, y = center
    draw.ellipse((round(x-radius), round(y-radius), round(x+radius), round(y+radius)),
                 fill=fill, outline=outline, width=max(1, round(width)))


def _panel(draw, box, fill, outline=None, radius=20, width=1):
    x, y, w, h = box
    draw.rounded_rectangle((round(x), round(y), round(x+w), round(y+h)),
                           radius=max(1, round(radius)), fill=fill, outline=outline, width=max(1, round(width)))


def _labels(scene: dict, defaults: tuple[str, str, str]) -> list[str]:
    labels = [str(v) for v in scene.get("labels", []) if str(v).strip()]
    return (labels + list(defaults[len(labels):]))[:3]


def _stamp(draw, text, box, palette, size):
    x, y, w, h = box
    _line(draw, ((x, y+h*.1), (x+w, y+h*.1)), palette["line"], 1)
    _text(draw, text, (x, y+h*.32, w, h*.68), size, palette["muted"], bold=True, lines=1)


def _neuron(draw, w, h, scene, p, time, c, s):
    labels = _labels(scene, ("Inputs", "Neuron responses", "A pattern is a clue"))
    level = scene.get("explanation_level", "technical")
    # Sparse edges and a single enlarged unit give the eye a route to follow.
    nodes = [[(.09*w, h*v) for v in (.22, .42, .62)],
             [(.33*w, h*v) for v in (.16, .37, .58, .75)],
             [(.62*w, h*v) for v in (.25, .50, .72)],
             [(.90*w, h*v) for v in (.33, .64)]]
    if level == "plain":
        nodes = [[(.13*w, h*v) for v in (.28,.59)],[(.50*w,.43*h)],
                 [(.87*w,h*v) for v in (.28,.59)]]
    elif level == "simple":
        nodes = [[(.12*w,.43*h)],[(.50*w,.43*h)],[(.88*w,.43*h)]]
    focus_layer = 2 if len(nodes)==4 else 1
    focus_node = 1 if len(nodes)==4 else 0
    focus_point = nodes[focus_layer][focus_node]
    for layer in range(len(nodes)-1):
        for n, start in enumerate(nodes[layer]):
            for j, end in enumerate(nodes[layer+1]):
                if level == "technical" or (n+j) % 2 == 0 or len(nodes)==3:
                    _line(draw, (start, end), c["line"], 1.5*s,
                          _phase(p, .015 + layer*.035, .15+layer*.05))
    pulse = (time*.22) % 1.
    for layer in range(len(nodes)-1):
        start = nodes[layer][layer % len(nodes[layer])]
        end = nodes[layer+1][(layer+1) % len(nodes[layer+1])]
        phase = (pulse * (len(nodes)-1) - layer)
        if 0 <= phase <= 1 and p > .10:
            point = (start[0] + (end[0]-start[0])*phase, start[1]+(end[1]-start[1])*phase)
            _circle(draw, point, 4*s, c["secondary"])
    for layer, values in enumerate(nodes):
        for j, point in enumerate(values):
            focus = layer == focus_layer and j == focus_node
            radius = (25 if level=="simple" and focus else 21 if focus else
                      15 if level=="simple" else 10)*s
            reveal = _phase(p, layer*.025, .14+layer*.035)
            _circle(draw, point, max(.5, radius*reveal), c["accent"] if focus else c["surface"],
                    c["accent"] if focus else c["foreground"], 2*s)
            if focus and p > .17:
                # Lens grows over the middle of the narration, then continues breathing.
                rr = (37 + 12*_phase(p, .20, .63) + 2*math.sin(time*1.1))*s
                _circle(draw, point, rr, outline=c["secondary"], width=1.5*s)
                _circle(draw, point, rr+12*s, outline=c["line"], width=1*s)
    _text(draw, labels[0], (.01*w, .005*h, .36*w, .15*h), 28*s, c["muted"], bold=True, lines=2)
    _text(draw, labels[1], (.57*w, .005*h, .41*w, .15*h), 28*s, c["foreground"], bold=True, lines=2)
    if p > .38:
        reveal = _phase(p, .38, .58)
        _line(draw, ((focus_point[0],focus_point[1]+68*s), (focus_point[0],.83*h)), c["secondary"], 1.5*s, reveal)
        _text(draw, labels[2], (.10*w, .855*h, .84*w, .13*h), 30*s,
              c["foreground"], bold=True, align="center", lines=2)


def _car(draw, x, y, size, color, back):
    _panel(draw, (x-size*.50, y-size*.24, size, size*.48), color, radius=size*.12)
    _panel(draw, (x-size*.20, y-size*.16, size*.32, size*.32), back, radius=size*.05)


def _traffic(draw, w, h, scene, p, time, c, s):
    labels = _labels(scene, ("A few photos", "An unseen situation", "Does the rule still work?"))
    signal_x, signal_y, sw, sh = w*.42, h*.06, w*.16, h*.58
    # The small photo and the dashed unseen frame establish known / novel context.
    for j in (1, 0):
        px, py = w*.015+j*7*s, h*.20-j*7*s
        _panel(draw, (px, py, w*.27, h*.31), c["surface"], c["line"], 10*s)
        if j == 0:
            _line(draw, ((px+w*.025, py+h*.22), (px+w*.245, py+h*.22)), c["line"], 2*s)
            _circle(draw, (px+w*.075, py+h*.082), 10*s, c["coral"])
            _car(draw, px+w*.15, py+h*.19, w*.095, c["foreground"], c["surface"])
    signal_ink = "#09252B"
    _panel(draw, (signal_x, signal_y, sw, sh), signal_ink, radius=sw*.31)
    colors = [c["coral"], "#F2C979", c["accent"]]
    active = 0 if p < .56 else 2 if p < .85 else 1
    for j, color in enumerate(colors):
        center = (signal_x+sw*.5, signal_y+sh*(.19+j*.31))
        r = min(sw*.25, sh*.09)
        _circle(draw, center, r, color if j == active else "#244047")
        if j == active:
            _circle(draw, center, r+5*s+1.5*s*math.sin(time*2), outline=color, width=1.5*s)
    _line(draw, ((w*.50, signal_y+sh), (w*.50, h*.75)), signal_ink, 5*s)
    _line(draw, ((w*.40, h*.76), (w*.60, h*.76)), c["line"], 2*s)
    ux, uy, uw, uh = w*.72, h*.20, w*.26, h*.31
    _dash(draw, (ux, uy), (ux+uw, uy), c["line"], 1.5*s, 7*s)
    _dash(draw, (ux, uy+uh), (ux+uw, uy+uh), c["line"], 1.5*s, 7*s)
    _dash(draw, (ux, uy), (ux, uy+uh), c["line"], 1.5*s, 7*s)
    _dash(draw, (ux+uw, uy), (ux+uw, uy+uh), c["line"], 1.5*s, 7*s)
    if p < .48:
        _text(draw, "?", (ux, uy+uh*.13, uw, uh*.72), 62*s, c["secondary"], bold=True, align="center", lines=1)
    else:
        _line(draw, ((ux+uw*.10, uy+uh*.74), (ux+uw*.90, uy+uh*.74)), c["line"], 2*s)
        offset = 0 if p < .56 else _phase(p, .56, .86)*uw*.16
        _car(draw, ux+uw*.46+offset, uy+uh*.62, uw*.34, c["secondary"], c["surface"])
    _line(draw, ((w*.31, h*.35), (w*.39, h*.35)), c["muted"], 2*s, _phase(p,.05,.25), True)
    _line(draw, ((w*.61, h*.35), (w*.69, h*.35)), c["secondary"], 2*s, _phase(p,.35,.58), True)
    _text(draw, labels[0], (.005*w, .56*h, .30*w, .17*h), 27*s, c["foreground"], bold=True, align="center", lines=3)
    _text(draw, labels[1], (.70*w, .56*h, .30*w, .17*h), 27*s, c["foreground"], bold=True, align="center", lines=3)
    _text(draw, labels[2], (.04*w, .80*h, .92*w, .11*h), 28*s, c["foreground"], bold=True, align="center", lines=2)
    _text(draw, "ANALOGY · ILLUSTRATIVE", (.04*w, .93*h, .92*w, .07*h), 22*s, c["muted"], bold=True, align="center", lines=1)


def _flow(draw, w, h, scene, p, time, c, s, *, analogy=False):
    labels = _labels(scene, ("Ask", "Check", "Repeat"))
    vertical = h > w*1.10
    centers = [(w*.22, h*(.14+j*.31)) for j in range(3)] if vertical else [
        (w*(.15+j*.35), h*.40) for j in range(3)]
    radius = min(w*(.087 if not vertical else .12), h*(.14 if not vertical else .10))
    active = min(2, int(_clamp(p)*3))
    for j in range(2):
        a, b = centers[j], centers[j+1]
        axis = (0, 1) if vertical else (1, 0)
        start = (a[0]+axis[0]*(radius+8*s), a[1]+axis[1]*(radius+8*s))
        end = (b[0]-axis[0]*(radius+9*s), b[1]-axis[1]*(radius+9*s))
        _line(draw, (start, end), c["line"], 2*s, _phase(p,.02+j*.13,.18+j*.16), True)
        # A repeating moving probe connects the three operations throughout the shot.
        t = (time*.30-j*.42) % 1
        if p > .12+j*.12:
            _circle(draw, (start[0]+(end[0]-start[0])*t, start[1]+(end[1]-start[1])*t),
                    4*s, c["secondary"])
    for j, (cx, cy) in enumerate(centers):
        reveal = _phase(p,.015+j*.08,.14+j*.09)
        radius_now = max(1., radius*(.90+.10*reveal))
        _circle(draw, (cx, cy), radius_now, c["surface"], c["accent"] if j == active else c["line"], 2*s)
        if j == active:
            _circle(draw, (cx,cy), radius+6*s+2*s*math.sin(time*1.7), outline=c["secondary"],width=1*s)
        icon_color = c["foreground"]
        if analogy or scene.get("explanation_level")=="simple":
            _text(draw, f"0{j+1}", (cx-radius,cy-radius*.48,radius*2,radius),radius*.65,
                  c["accent"] if j==active else icon_color,bold=True,align="center",lines=1)
        elif j == 0:
            # A trace represents a predicted signal; no numeric or measured values.
            points = [(cx-radius*.52,cy+radius*.29), (cx-radius*.18,cy-radius*.20),
                      (cx+radius*.13,cy+radius*.04),(cx+radius*.48,cy-radius*.34)]
            draw.line(points,fill=icon_color,width=max(1,round(3*s)),joint="curve")
            for point in (points[0], points[-1]):
                _circle(draw,point,3*s,c["secondary"])
        elif j == 1:
            _line(draw,((cx-radius*.51,cy-radius*.20),(cx+radius*.45,cy-radius*.20)),icon_color,2*s,arrow=True)
            _line(draw,((cx+radius*.51,cy+radius*.22),(cx-radius*.45,cy+radius*.22)),c["secondary"],2*s,arrow=True)
        else:
            for k in range(3):
                px = cx+radius*(-.45+k*.43)
                draw.line(((px-2*s,cy+radius*.40),(px-2*s,cy-radius*(.13+k*.16))),fill=icon_color,width=max(2,round(3*s)))
                draw.line(((px+4*s,cy+radius*.40),(px+4*s,cy-radius*(.22+k*.13))),fill=c["secondary"],width=max(2,round(3*s)))
        if vertical:
            _text(draw,labels[j],(cx+radius+16*s,cy-radius*.50,
                  w-cx-radius-25*s,radius*1.7),28*s,c["foreground"],bold=True,lines=3)
        else:
            _text(draw,f"0{j+1}",(cx-radius,cy-radius-37*s,radius*2,31*s),21*s,c["muted"],align="center",bold=True,lines=1)
            _text(draw,labels[j],(cx-w*.15,cy+radius+24*s,w*.30,h*.25),28*s,c["foreground"],bold=True,align="center",lines=3)
    _stamp(draw, "ANALOGY · ILLUSTRATIVE" if analogy else "SCHEMATIC · NO MEASURED DATA",
           (.02*w,.90*h,.96*w,.10*h),c,22*s)


def _comparison(draw, w, h, scene, p, time, c, s):
    labels = _labels(scene,("Original", "Alternative", "Compare the behavior"))
    mode = comparison_mode(scene)
    gap, cw = w*.09, w*.455
    _line(draw,((w*.5,h*.025),(w*.5,h*.75)),c["line"],1*s)
    profiles = toy_profiles(p)
    active = 0 if p < .46 else 1
    for side in range(2):
        x = side*(cw+gap)
        color = c["secondary"] if side == 0 else c["accent"]
        _text(draw, f"0{side+1}",(x,.005*h,cw,.07*h),21*s,c["muted"],bold=True,lines=1)
        _text(draw,labels[side],(x,.105*h,cw,.20*h),29*s,c["foreground"],bold=True,lines=3)
        if side == active:
            _line(draw,((x,.34*h),(x+cw*.72,.34*h)),color,3*s,_phase(p,.015+side*.43,.23+side*.43))
        baseline = h*.685
        if mode == "probability":
            _line(draw,((x+cw*.04,baseline),(x+cw*.96,baseline)),c["line"],1*s)
            for j, value in enumerate(profiles[side]):
                px = x+cw*(.17+j*.33)
                bh = h*.43*value*_phase(p,.04+side*.13,.24+side*.17)
                _panel(draw,(px-cw*.09,baseline-bh,cw*.18,max(1,bh)),color,radius=5*s)
                if scene.get("explanation_level") != "simple":
                    _text(draw,"ABC"[j],(px-cw*.12,baseline+8*s,cw*.24,.07*h),21*s,c["muted"],align="center",lines=1)
        elif mode == "activation" and side == 0:
            for j in range(3):
                px, py = x+cw*(.16+j*.34), h*.53
                rr = cw*.09
                _circle(draw,(px,py),rr,color if j==0 else c["surface"],color if j==0 else c["line"],2*s)
                if j == 0:
                    _circle(draw,(px,py),rr+5*s+2*s*math.sin(time*2),outline=color,width=1*s)
                _text(draw,"ON" if j==0 else "OFF",(px-cw*.17,baseline,cw*.34,.08*h),21*s,c["muted"],align="center",bold=True,lines=1)
        elif mode == "activation":
            for j,value in enumerate((.27,.88)):
                px = x+cw*(.27+j*.47)
                bh = h*.32*value*_phase(p,.23,.61)
                _panel(draw,(px-cw*.095,baseline-bh,cw*.19,max(1,bh)),color,radius=5*s)
                _text(draw,"weak" if j==0 else "strong",(px-cw*.23,baseline+8*s,cw*.46,.08*h),22*s,c["muted"],align="center",lines=1)
        else:
            # Nonquantitative marks for a generic comparison; no implied axes.
            for j in range(3):
                px, py = x+cw*(.16+j*.34), h*.54
                rr = cw*.075
                if side == 0:
                    _circle(draw,(px,py),rr,color)
                else:
                    angle = .25*math.sin(time*.45+j)
                    points = [(px+math.cos(angle+k*math.pi/2)*rr*1.3,py+math.sin(angle+k*math.pi/2)*rr*1.3) for k in range(4)]
                    draw.polygon(points,fill=color)
    _text(draw,labels[2],(.02*w,.80*h,.96*w,.12*h),28*s,c["foreground"],bold=True,align="center",lines=2)
    _text(draw,"TOY EXAMPLE · NO MEASURED DATA" if mode != "generic" else "ILLUSTRATIVE COMPARISON",
          (.02*w,.92*h,.96*w,.08*h),22*s,c["muted"],bold=True,align="center",lines=1)


def _takeaway(draw,w,h,scene,p,time,c,s):
    labels = _labels(scene,("A useful check", "Keep the limits", "Read the source"))
    cx,cy,r = w*.50,h*.37,min(w*.23,h*.29)
    # A bounded magnifying lens communicates scope, rather than a safety badge.
    for orbit in range(0 if scene.get("explanation_level")=="simple" else 2):
        rr = r*(1.35+orbit*.30)
        draw.arc((cx-rr,cy-rr*.58,cx+rr,cy+rr*.58),20+orbit*70,175+orbit*65,fill=c["line"],width=max(1,round(s)))
        angle = time*(.24 if orbit==0 else -.16)+orbit*2
        _circle(draw,(cx+math.cos(angle)*rr,cy+math.sin(angle)*rr*.58),4*s,
                c["secondary"] if orbit==0 else c["coral"])
    _circle(draw,(cx,cy),r,c["surface"],c["foreground"],3*s)
    _circle(draw,(cx,cy),r-8*s,outline=c["line"],width=1*s)
    positions = [(cx-r*.46,cy-r*.20),(cx-r*.09,cy+r*.36),(cx+r*.35,cy-r*.32)]
    for start,end in ((positions[0],positions[1]),(positions[1],positions[2]),(positions[0],positions[2])):
        _line(draw,(start,end),c["line"],2*s,_phase(p,.02,.23))
    for j,point in enumerate(positions):
        _circle(draw,point,9*s,c["accent"] if j == min(2,int(p*3)) else c["secondary"])
    _line(draw,((cx+r*.72,cy+r*.72),(cx+r*1.22,cy+r*1.22)),c["foreground"],9*s)
    _text(draw,labels[0],(.03*w,.73*h,.94*w,.11*h),30*s,c["foreground"],bold=True,align="center",lines=2)
    _text(draw," / ".join(labels[1:]),(.03*w,.875*h,.94*w,.12*h),22*s,c["muted"],align="center",lines=2)


def _source(draw,w,h,scene,p,time,c,s,credits):
    margin = min(w*.035,h*.055)
    # Publication-style block: a visible paper edge, oversized type and rule work.
    _panel(draw,(margin+9*s,margin+9*s,w-2*margin-9*s,h-2*margin-9*s),c["line"],radius=3*s)
    _panel(draw,(margin,margin,w-2*margin-9*s,h-2*margin-9*s),c["surface"],c["line"],radius=3*s)
    ix,iy,iw,ih = margin+26*s,margin+25*s,w-2*margin-61*s,h-2*margin-55*s
    _text(draw,"READ THE ORIGINAL",(ix,iy,iw,ih*.07),21*s,c["muted"],bold=True,lines=1)
    _line(draw,((ix,iy+ih*.135),(ix+iw,iy+ih*.135)),c["foreground"],2*s,_phase(p,.015,.20))
    title = str(credits.get("title") or scene.get("heading") or "Research source")
    _text(draw,title,(ix,iy+ih*.22,iw,ih*.42),36*s,c["foreground"],bold=True,lines=4)
    authors = credits.get("authors") or []
    if isinstance(authors,str):
        authors=[authors]
    byline = ", ".join(str(v) for v in authors[:4])+(" et al." if len(authors)>4 else "")
    _text(draw,byline,(ix,iy+ih*.72,iw,ih*.11),24*s,
          _blend(c["surface"],c["muted"],_phase(p,.15,.46)),lines=2)
    domain = urlparse(str(credits.get("source_url",""))).netloc.removeprefix("www.")
    _text(draw,domain,(ix,iy+ih*.91,iw*.83,ih*.08),23*s,
          _blend(c["surface"],c["foreground"],_phase(p,.41,.72)),bold=True,lines=1)
    _line(draw,((ix+iw*.89,iy+ih*.945),(ix+iw*.99,iy+ih*.945)),c["foreground"],2*s,
          _phase(p,.57,.92),arrow=True)


def draw_diagram(image, scene: dict, box: tuple[float,float,float,float], elapsed: float,
                 duration: float, portrait: bool, credits: dict, theme: dict, beat: float=0) -> None:
    """Paint one responsive animation into a clipped, renderer-owned safe box.

    ``elapsed / duration`` drives gradual focus changes over the complete scene;
    ``elapsed`` supplies continuous motion between those changes. ``beat`` is a
    reserved optional cue from a future narration-alignment stage.
    """
    if Image is None:
        raise RuntimeError("Video diagrams require Pillow; install requirements-video.txt")
    x,y,w,h = (round(v) for v in box)
    if w < 8 or h < 8:
        return
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(elapsed):
        raise ValueError("Diagram timing must be finite, with a positive duration")
    c = dict(theme)
    c["surface"] = c.get("surface",c.get("panel",c["background"]))
    c["secondary"] = c.get("secondary",c.get("aqua",c["accent"]))
    # Box-relative typography is consistent across 1080p and preview resolutions.
    scale = min(w/700,h/470)
    canvas = Image.new("RGBA",(w,h),(0,0,0,0))
    draw = ImageDraw.Draw(canvas)
    progress = _clamp(elapsed/duration)
    kind = scene.get("visual","neuron")
    if kind == "neuron":
        _neuron(draw,w,h,scene,progress,elapsed,c,scale)
    elif kind == "analogy":
        if analogy_mode(scene) == "traffic_light":
            _traffic(draw,w,h,scene,progress,elapsed,c,scale)
        else:
            _flow(draw,w,h,scene,progress,elapsed,c,scale,analogy=True)
    elif kind == "comparison":
        _comparison(draw,w,h,scene,progress,elapsed,c,scale)
    elif kind == "test":
        _flow(draw,w,h,scene,progress,elapsed,c,scale)
    elif kind == "takeaway":
        _takeaway(draw,w,h,scene,progress,elapsed,c,scale)
    else:
        _source(draw,w,h,scene,progress,elapsed,c,scale,credits)
    image.paste(canvas,(x,y),canvas)
