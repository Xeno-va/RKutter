"""Turn drawing paths (mm) into RK laser card instructions, the way RK-CAD does.

Follows ../PROTOCOL.md: the correction pipeline (grids, area scale and
offset, flips, mm -> mirror steps, centre 32767), laser set-up per laser
type, then jump / laser on / mark / laser off with RK-CAD's delays. Fills
follow RK-CAD's own hatch routine (decoded, and matched against its captures).

Values marked UNVERIFIED below are read from the code, never seen on the
laser. Nothing here has been run on the board.
"""
import math
import struct
from dataclasses import dataclass, field

import pyclipper

import rklaser as rk

OP_PA_LOW = 0x0F
OP_FPK_LOW = 0x0A
OP_PWM_START_TIME = 0x11
OP_PWM_ADD_TIME = 0x12
OP_PWM_ADD_NUMBER = 0x13
OP_PWM_OPEN = 0x15
OP_PWM_CLOSE = 0x16

CENTRE = 32767
FIELD_UNITS = 51200.0  # mirror steps across the whole lens field


def op32(code, value):
    return rk.instruction(code, struct.pack(">I", int(value) & 0xFFFFFFFF))


def op16(code, value):
    return rk.instruction(code, struct.pack(">H", int(value) & 0xFFFF))


@dataclass
class Machine:
    """Per-laser settings RK-CAD keeps in its settings file (laser PC copy wins over these defaults)."""
    field_mm: float = 110.0  # UNVERIFIED: the user's lens
    laser_type: int = 2  # 0 fiber, 1 CO2, 2 YAG-style; UNVERIFIED for the user's UV laser
    # RK-CAD's [xiaozheng] settings. Either the correction grids are on, or scale and the
    # barrel / tilt / trapezoid factors are used: never both (decoded, 0x1402f3ec0).
    flip_x: bool = False  # x_reverse
    flip_y: bool = False  # y_reverse
    swap_xy: bool = False  # xy_swap
    grids_on: bool = False  # xiaozheng_enable
    # [(n, {point_no: (dx, dy)}), ...] from grid_from_measured: 3x3, then 5x5, then the custom
    # grids. RK-CAD chains them: each is used only if every one before it is switched on.
    grids: list = field(default_factory=list)
    scale_x: float = 1.0  # xaxis_scale / 100
    scale_y: float = 1.0
    barrel_x: float = 1.0  # xaxis_tong
    barrel_y: float = 1.0
    tilt_x: float = 1.0  # xaxis_qingxie
    tilt_y: float = 1.0
    trapezoid_x: float = 1.0  # xaxis_ti
    trapezoid_y: float = 1.0
    offset_x: float = 0.0  # added after the grids in one position mode; UNVERIFIED which
    offset_y: float = 0.0
    power_curve: tuple = (10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 95)  # power0 .. power100
    pa_polarity: int = 0  # YAG-style: which level means "on"
    mo_delay_ms: int = 8
    mopa: bool = False  # [fiberconfig] enablemopa: adjustable pulse width


@dataclass
class Pen:
    """One colour's settings (the panel row plus RK-CAD's per-pen delays, factory values)."""
    power: float = 10.0  # percent
    frequency_khz: float = 40.0
    pulse_width: int = 10  # ns; sent only in MOPA mode (captured)
    current_pct: float = 50.0  # MOPA pulse timing share, RK-CAD's per-pen "current" (factory 50)
    speed_mm_s: float = 1000.0
    passes: int = 1
    mark_step_mm: float = 0.05  # captured: sets time per point, not the spacing
    step_delay_us: int = 10
    open_delay_us: int = -100
    close_delay_us: int = 150
    finish_delay_us: int = 150
    corner_delay_us: int = 50
    jump_speed_mm_s: float = 4000.0  # captured: ~0.16 mm per 40 us step
    min_jump_delay_us: int = 200  # factory minskip_delay; captured: jumps under 10 mm end at time 20
    max_jump_delay_us: int = 500  # factory maxskip_delay; captured: longer jumps end at time 50
    max_jump_distance_mm: float = 10.0  # factory maxskip_distance
    fill_spacing_mm: float = 0.05
    fill_angle: float = 0.0
    fill_margin_mm: float = 0.0
    cross_fill: bool = False


# ------------------------------------------------------------ correction

def machine_from_ini(path):
    """A Machine from RK-CAD's own settings file (lasermarkparam.ini, copied off the laser PC),
    using the sections RK-CAD itself reads: [xiaozheng] lens settings and grids, [laserconfig]
    power curve and laser type, [fiberconfig] master-oscillator delay."""
    import configparser
    c = configparser.ConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read(path, encoding="utf-8")
    z = c["xiaozheng"]
    f = lambda k, d=0.0: float(z.get(k, d))
    on = lambda k: int(float(z.get(k, 0))) == 1
    F = f("mark_scope", 100.0)
    grids = []
    # Decoded: each grid is used only if every one before it is switched on.
    for flag, n in (("point9_enable", 3), ("point25_enable", 5)):
        if not on(flag):
            break
        sec = c[f"{n * n}point"]
        grids.append(grid_from_measured(n, {k: (float(sec[f"{k}x"]), float(sec[f"{k}y"]))
                                            for k in range(1, n * n + 1)}, F))
    else:
        if on("points_enable"):  # UNVERIFIED which section the custom grid uses; 7x7 is RK-CAD's stock one
            sec = c["49point"]
            grids.append(grid_from_measured(7, {k: (float(sec[f"{k}x"]), float(sec[f"{k}y"]))
                                                for k in range(1, 50)}, F))
    lc = c["laserconfig"] if c.has_section("laserconfig") else {}
    curve = tuple(float(lc.get(f"power{p}", d)) for p, d in zip(range(0, 101, 10), Machine.power_curve))
    return Machine(field_mm=F, laser_type=int(lc.get("activelasertype", 0)),
                   flip_x=on("x_reverse"), flip_y=on("y_reverse"), swap_xy=on("xy_swap"),
                   grids_on=on("xiaozheng_enable"), grids=grids,
                   scale_x=f("xaxis_scale", 100.0) / 100.0, scale_y=f("yaxis_scale", 100.0) / 100.0,
                   barrel_x=f("xaxis_tong", 1.0), barrel_y=f("yaxis_tong", 1.0),
                   tilt_x=f("xaxis_qingxie", 1.0), tilt_y=f("yaxis_qingxie", 1.0),
                   trapezoid_x=f("xaxis_ti", 1.0), trapezoid_y=f("yaxis_ti", 1.0), power_curve=curve,
                   mo_delay_ms=int(float(c["fiberconfig"].get("modelay", 8))) if c.has_section("fiberconfig") else 8,
                   mopa=c.has_section("fiberconfig") and int(float(c["fiberconfig"].get("enablemopa", 0))) == 1)


def read_correction_table(path):
    """RK-CAD's correction file as text: one grid point per line, number then measured X and Y,
    separated by any whitespace (decoded reader, 0x1401eef20; it also takes Excel files)."""
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        parts = line.split()
        try:
            out[int(float(parts[0]))] = (float(parts[1]), float(parts[2]))
        except (IndexError, ValueError):
            continue  # header rows such as "NO. X Y"
    return out


def grid_from_measured(n, measured, F):
    """A correction grid from RK-CAD's stored table ([9point], [25point], ...: where each grid point
    actually landed, in mm, numbered 1..n*n with rows from the top). Fitted to captures:
    3x3 corrects each axis by ideal x (ideal / measured - 1); 5x5 by ideal - measured, except
    that points on the centre lines get none along that axis. Larger grids are assumed to
    follow 5x5 (UNVERIFIED)."""
    cell, table = F / (n - 1), {}
    for k, (mx, my) in measured.items():
        ix, iy = -F / 2 + ((k - 1) % n) * cell, F / 2 - ((k - 1) // n) * cell
        if n == 3:
            table[k] = (ix * (ix / mx - 1.0) if ix else 0.0, iy * (iy / my - 1.0) if iy else 0.0)
        else:
            table[k] = (0.0 if ix == 0 else ix - mx, 0.0 if iy == 0 else iy - my)
    return n, table


def _grid(n, table, x, y, F):
    """One correction grid: bilinear blend of the four corner corrections (RK-CAD's grid step)."""
    half, cell = F / 2.0, F / (n - 1)
    px, py = x + half, y + half

    def index(v):
        if v <= 1e-8:
            return 0
        if v >= F - 1e-8:
            return n - 2
        return min(int(v / cell), n - 2)
    i, j = index(px), index(py)
    # Grid points are numbered 1..n*n with rows counted from the top.
    def point(ci, cj):
        k = cj * n + ci
        return table.get((n - k // n - 1) * n + 1 + k % n, (0.0, 0.0))
    x0, x1, y0, y1 = i * cell, (i + 1) * cell, j * cell, (j + 1) * cell
    c00, c10, c01, c11 = point(i, j), point(i + 1, j), point(i, j + 1), point(i + 1, j + 1)
    tx, ty = (px - x0) / cell, (py - y0) / cell
    dx = (1 - ty) * ((1 - tx) * c00[0] + tx * c10[0]) + ty * ((1 - tx) * c01[0] + tx * c11[0])
    dy = (1 - ty) * ((1 - tx) * c00[1] + tx * c10[1]) + ty * ((1 - tx) * c01[1] + tx * c11[1])
    return px + dx - half, py + dy - half


def _shape(m, x, y):
    """RK-CAD's distortion factors when the grids are off (decoded, 0x1402f4110), kept literal."""
    x, y = m.scale_x * x, m.scale_y * y
    r = m.field_mm * 0.5
    x0 = x
    if abs(x) >= 1e-8 and abs(m.barrel_x - 1.0) >= 1e-8:
        x = (2.0 - m.barrel_x) * x - (1.0 - m.barrel_x) * x * y * y * (1.0 / (r * r))
    if abs(y) >= 1e-8 and abs(m.barrel_y - 1.0) >= 1e-8:
        y = (2.0 - m.barrel_y) * y - (1.0 - m.barrel_y) * y * x0 * x0 * (1.0 / (r * r))
    xb = x
    if abs(m.tilt_x - 1.0) >= 1e-8:
        x = (1.0 - m.tilt_x) * y + xb
    if abs(m.tilt_y - 1.0) >= 1e-8:
        y = (1.0 - m.tilt_y) * xb + y
    xt = x
    if abs(m.trapezoid_x - 1.0) >= 1e-8:
        x = (1.0 / r) * xt * (1.0 - m.trapezoid_x) * y + xt
    if abs(m.trapezoid_y - 1.0) >= 1e-8:
        y = (1.0 / r) * y * (1.0 - m.trapezoid_y) * xt + y
    return x, y


def to_mirror(m, x, y):
    """mm (field centred on 0, 0) -> 16-bit mirror X, Y, in RK-CAD's order (0x1402f5080)."""
    if m.grids_on:
        for n, table in m.grids:
            x, y = _grid(n, table, x, y, m.field_mm)
    x, y = x + m.offset_x, y + m.offset_y
    if not m.grids_on:
        x, y = _shape(m, x, y)
    if m.flip_x:
        x = -x
    if m.flip_y:
        y = -y
    if m.swap_xy:
        x, y = y, x
    k = FIELD_UNITS / m.field_mm
    # Captured: no range check here; a jump from the corner the board reports runs outside the
    # field and wraps to 16 bits. The drawing itself is checked against the field before building.
    return int(k * x + CENTRE), int(k * y + CENTRE)


def power_value(m, pct, sixteen_bit=False):
    i = max(0, min(int(pct / 10.0), 9))
    lo, hi = m.power_curve[i], m.power_curve[i + 1]
    p = min((hi - lo) * ((pct - i * 10) / 10.0) + lo, 99.99999)
    return max(1, int(p * (655.35 if sixteen_bit else 2.55)))


# ------------------------------------------------------------ instructions

def laser_setup(m, pen):
    """Exactly as captured from RK-CAD (fiber type): pulse generator, power latch, master oscillator.
    MOPA mode (captured 2026-10-30): first red pointer on, pulse width in ns, 50 ms, red pointer
    off; and the pulse start/add times become current% x cycle x 0.5 (decoded 0x1402ec180)."""
    cycle = round(25000.0 / pen.frequency_khz)  # 25 MHz pulse clock (captured: 30 kHz -> 833)
    if m.laser_type == 0:
        pre, start = [], 1
        if m.mopa:
            pre = [rk.instruction(rk.OP_RED_ON), op16(rk.OP_PULSE_WIDTH, int(pen.pulse_width)), rk.delay(5000),
                   rk.instruction(rk.OP_RED_OFF)]
            start = int(pen.current_pct * 0.01 * cycle * 0.5)
        return pre + [op32(rk.OP_PWM_CYCLE_TIME, cycle), op32(OP_PWM_START_TIME, start), op32(OP_PWM_ADD_TIME, start),
                      op32(OP_PWM_ADD_NUMBER, 1), op32(OP_PWM_OPEN, 1),
                      op16(rk.OP_LASER_POWER, power_value(m, pen.power)),
                      rk.instruction(rk.OP_POWER_LOCK_HIGH), rk.delay(1), rk.instruction(rk.OP_POWER_LOCK_LOW),
                      rk.instruction(rk.OP_MO_ON), rk.delay(m.mo_delay_ms * 100)]
    # CO2 / YAG-style: power becomes pulse on-time. Read from the code, not yet captured.
    on = round(power_value(m, pen.power) * cycle / 255.0 * 0.5)
    return [op32(rk.OP_PWM_CYCLE_TIME, cycle), op32(OP_PWM_START_TIME, on), op32(OP_PWM_ADD_TIME, on),
            op32(OP_PWM_ADD_NUMBER, 1)]


def laser_on(m, pen):
    on = op32(OP_PA_LOW, 1) if (m.laser_type == 2 and m.pa_polarity == 1) else op32(rk.OP_PA_HIGH, 1)
    if m.laser_type == 1:
        on = op32(OP_PWM_OPEN, 1)
    return [on, rk.delay(max(1, abs(pen.open_delay_us) // 10))]  # captured: open -100 us -> delay 10


def laser_off(m, pen):
    off = op32(rk.OP_PA_HIGH, 1) if (m.laser_type == 2 and m.pa_polarity == 1) else op32(OP_PA_LOW, 1)
    if m.laser_type == 1:
        off = op32(OP_PWM_CLOSE, 1)
    return [rk.delay(max(1, pen.finish_delay_us // 10)), off, rk.delay(max(1, pen.close_delay_us // 10))]


def from_mirror(m, mx, my):
    """16-bit mirror X, Y -> mm, undoing flips, swap and offset only; grids and distortion factors
    are not undone (UNVERIFIED: how RK-CAD maps the board's reported position back)."""
    x, y = (mx - CENTRE) * m.field_mm / FIELD_UNITS, (my - CENTRE) * m.field_mm / FIELD_UNITS
    if m.swap_xy:
        x, y = y, x
    if m.flip_x:
        x = -x
    if m.flip_y:
        y = -y
    return x - m.offset_x, y - m.offset_y


def job_start(m, pen, here):
    """Captured: every job opens with the red pointer off and a move to where the board reported the
    mirrors (mapped back to mm, then through the lens maths like any point, at the first pen's point
    time); the first jump then steps on from there."""
    return [rk.instruction(rk.OP_RED_OFF), _move(m, *here, _ticks(pen))]


def job_wrap_up(m):
    """Captured: the batch after every job, laser held off, mirrors parked mid-field, red pointer on."""
    return [op32(OP_PA_LOW, 1)] + job_end(m) + red_stop_block(m)


def job_end(m):
    return [rk.instruction(rk.OP_MO_OFF), op32(OP_PWM_CLOSE, 1)] if m.laser_type == 0 else []


def _ticks(pen):
    """Time per point in the board's 10 us units: mark_step / speed, rounded up (captured: 33 us -> 4)."""
    return max(1, math.ceil(pen.mark_step_mm / pen.speed_mm_s * 1e5 - 1e-9))


def _move(m, x, y, t):
    mx, my = (v & 0xFFFF for v in to_mirror(m, x, y))
    return rk.mark_xy2(t, mx - 0x10000 if mx > 0x7FFF else mx, my - 0x10000 if my > 0x7FFF else my, 1000)


MARK_TAIL = 0.3  # fitted to 470 captured lines (+-0.03): a marked line's last part shorter than this
                 # many steps is folded into the step before; jumps always take the short step


def _steps(a, b, spacing, tail=1e-9):
    """Points from a (exclusive) to b (inclusive) at fixed `spacing`, landing on b (captured: RK-CAD
    steps a fixed distance rather than dividing the line evenly)."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length == 0:
        return [b]
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    q = length / spacing
    n = math.floor(q)
    if q - n > tail or n == 0:
        n += 1
    return [(a[0] + ux * spacing * k, a[1] + uy * spacing * k) for k in range(1, n)] + [b]


def _turn(a, b, c):
    v1, v2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if not n1 or not n2:
        return 0.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


def mark_paths(m, pen, paths, start=(0.0, 0.0), setup_done=False, start_sent=False):
    """Instructions for polylines (mm, field centred on 0, 0) with one pen, in RK-CAD's captured order.
    start_sent: the jump's starting point is already in the stream (job_start puts it there)."""
    t = _ticks(pen)
    spacing = pen.speed_mm_s * t * 1e-5  # captured: 1500 mm/s x 40 us = 0.06 mm
    jump_spacing = pen.jump_speed_mm_s * t * 1e-5
    out, here = [], start
    for _ in range(pen.passes):
        for path in paths:
            if len(path) < 2:
                continue
            # Captured: a jump re-sends where it starts, steps on, and its last move carries the
            # settle time: the short jump delay up to max_jump_distance, the long one beyond.
            if not start_sent:
                out.append(_move(m, *here, t))
            start_sent = False
            jump = _steps(here, path[0], jump_spacing)
            out += [_move(m, x, y, t) for x, y in jump[:-1]]
            far = math.hypot(path[0][0] - here[0], path[0][1] - here[1]) > pen.max_jump_distance_mm
            out.append(_move(m, *path[0], max(1, (pen.max_jump_delay_us if far else pen.min_jump_delay_us) // 10)))
            if not setup_done:
                out += laser_setup(m, pen)
                setup_done = True
            out += laser_on(m, pen)
            for i in range(1, len(path)):
                # Captured: RK-CAD re-sends each segment's start point before stepping along it.
                out += [_move(m, x, y, t) for x, y in [path[i - 1]] + _steps(path[i - 1], path[i], spacing, MARK_TAIL)]
                if i + 1 < len(path) and _turn(path[i - 1], path[i], path[i + 1]) > 90:
                    out.append(rk.delay(max(1, pen.corner_delay_us // 10)))
            out += laser_off(m, pen)
            here = path[-1]
    return out


def build_job(m, layers):
    """layers: [(pen, paths)]. Whole job, ending as RK-CAD does."""
    out, here = [], (0.0, 0.0)
    for pen, paths in layers:
        out += mark_paths(m, pen, paths, here)
        if paths:
            here = paths[-1][-1]
    return out + job_end(m)


# ------------------------------------------------------------ fills

SCALE = 10000.0  # Clipper works in integers: 0.1 micron


def _scanlines(polys, spacing, theta, average):
    """RK-CAD's getScanline (RS_Hatch::update, decoded): one anchor point and angle per line.
    Shallow angles step up from the bottom of the bounding box through its centre x; steep ones
    step across from the left through its centre y; either range is widened by 1.1 x the slope.
    Kept in the same double arithmetic and order so lines that land exactly on an edge do too."""
    th = math.atan2(math.sin(theta), math.cos(theta))
    if abs(abs(th) - math.pi) <= 1e-05:
        th = 0.0
    xs = [x for p in polys for x, _ in p]
    ys = [y for p in polys for _, y in p]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    if abs(th) <= 0.7853981633974483:
        step = spacing / math.cos(th)
        length = (xmax - xmin) / math.cos(th)
        widen = math.sin(th) * abs(length) * 1.1
        lo, hi = ymin - abs(widen), abs(widen) + ymax
        at = lambda v: (cx, v)
    else:
        step = spacing / math.sin(th)
        length = (ymax - ymin) / math.sin(th)
        widen = math.cos(th) * abs(length) * 1.1
        lo, hi = xmin - abs(widen), abs(widen) + xmax
        at = lambda v: (v, cy)
    step = abs(step)
    if average:  # averageFill: stretch the spacing so the lines fill the range evenly
        step = (hi - lo - 1e-05) / int((hi - lo) / step)
    v, out = lo, []
    while v <= hi:
        out.append(at(v))
        v += step
    return th, out


def hatch(polys, spacing, angle_deg=0.0, margin=0.0, cross=False, average=False):
    """Fill lines for closed polygons (mm, y up, vertices in drawing order), in RK-CAD's order.
    Matched line for line, ends and direction included, against 20 captured RK-CAD fills
    (squares, rectangles drawn both ways round, groups; 0-270 deg; 0.5 and 1 mm spacing):
      - all of a colour's shapes are hatched as one, from their combined bounding box;
      - steep lines (over 45 deg from flat) always run upward;
      - a flatter line through one shape runs between its two crossings in the order the
        shape's edges are walked (an edge's start counts, its end does not); one crossing
        more than that is cut at its crossings sorted along the line, and runs with the angle;
      - a line lying exactly on a straight edge marks that edge (left to right, or upward)
        when the shape is anticlockwise, and nothing when it is clockwise. One capture broke
        this (a 90 deg line 2e-15 mm off a square's edge, not marked): RK-CAD's own float
        noise from importing decides it there, which no drawing can reproduce.
    Margin (inset) is done with Clipper: UNVERIFIED against RK-CAD's own inset."""
    if margin:
        pc = pyclipper.PyclipperOffset()
        for p in polys:
            pc.AddPath([(int(x * SCALE), int(y * SCALE)) for x, y in p], pyclipper.JT_MITER,
                       pyclipper.ET_CLOSEDPOLYGON)
        polys = [[(x / SCALE, y / SCALE) for x, y in p] for p in pc.Execute(-margin * SCALE)]
    polys = [list(p[:-1]) if len(p) > 1 and p[0] == p[-1] else list(p) for p in polys]
    polys = [p for p in polys if len(p) > 2]
    if not polys:
        return []
    lines = []
    # crossFill: a second pass at a quarter turn (getScanline's param_4 x pi/2).
    for theta in ([math.radians(angle_deg), math.radians(angle_deg) + math.pi / 2] if cross
                  else [math.radians(angle_deg)]):
        th, anchors = _scanlines(polys, spacing, theta, average)
        ca, sa = math.cos(th), math.sin(th)
        ca, sa = (0.0 if abs(ca) < 1e-12 else ca), (0.0 if abs(sa) < 1e-12 else sa)  # true 90 deg
        steep = abs(th) > 0.7853981633974483
        along = (lambda q: q[1]) if steep else (lambda q: q[0])
        for px, py in anchors:
            n = lambda q: -(q[0] - px) * sa + (q[1] - py) * ca
            hits, on_edge, ends = [], [], []
            for p in polys:
                ccw = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(p, p[1:] + p[:1])) > 0
                # A vertex within 1e-9 of the line counts as on it (captured: a line through a
                # corner starts there). An edge lying along the line but a hair inside the shape
                # is an ordinary line there (captured), so its two ends keep their exact side.
                raw = [n(q) for q in p]
                along_line, inward = [], set()
                for i in range(len(p)):
                    j = (i + 1) % len(p)
                    if abs(raw[i]) < 1e-9 and abs(raw[j]) < 1e-9:
                        a, b = p[i], p[j]
                        side = -raw[i] * (sa * (b[1] - a[1]) + ca * (b[0] - a[0])) * (1 if ccw else -1)
                        (inward.update((i, j)) if side > 0 else along_line.append(i))
                val = [v if k in inward or abs(v) >= 1e-9 else 0.0 for k, v in enumerate(raw)]
                for i, a in enumerate(p):
                    j = (i + 1) % len(p)
                    b, na, nb = p[j], val[i], val[j]
                    if i in along_line:
                        if ccw:
                            on_edge.append(sorted([a, b], key=along))
                        ends += [a, b]
                    elif (na <= 0 < nb or nb < 0 <= na) and not (abs(raw[i]) < 1e-9 and abs(raw[j]) < 1e-9):
                        u = na / (na - nb)
                        hits.append((a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])))
            if ends:  # an edge on the line: its ends, met again by its neighbours, make no other line
                hits = [q for q in hits if all(math.dist(q, e) > 1e-6 for e in ends)]
            if steep:
                hits.sort(key=along)
            elif len(hits) > 3:
                hits.sort(key=lambda q: q[0] * ca + q[1] * sa)
            pieces = [[a, b] for a, b in zip(hits[0::2], hits[1::2]) if math.dist(a, b) > 1e-6]
            lines += on_edge + pieces
    return lines


# ------------------------------------------------------------ red light (captured)

def red_frame_block(m, bounds, here=None, speed_mm_s=2000.0, mark_step_mm=0.05, block=10000):
    """One block of RK-CAD's continuous red-light frame: laps of the bounding box from its top-left
    corner, a 150 us pause at the start of each lap, moves with the laser off, END at 10,000.
    Captured: steps ~0.06 mm at time 3 (2000 mm/s), box = the selection's bounding box."""
    x0, y0, x1, y1 = bounds
    return red_frame_paths(m, [[(x0, y1), (x1, y1), (x1, y0), (x0, y0), (x0, y1)]], here, speed_mm_s,
                           mark_step_mm, block)


def red_frame_paths(m, paths, here=None, speed_mm_s=2000.0, mark_step_mm=0.05, block=10000):
    """Laps of the given outlines in the red light, built the way the captured bounding-box frame
    is (150 us pause per lap, moves with the laser off at 2000 mm/s, END at 10,000). With one box
    path this is that capture exactly. UNVERIFIED for the outlines themselves: RK-CAD's
    outline-display red light has not been captured."""
    pen = Pen(speed_mm_s=speed_mm_s, mark_step_mm=mark_step_mm)
    t = _ticks(pen)
    spacing = speed_mm_s * t * 1e-5
    out, last = [], here or paths[0][0]
    while len(out) < block - 1:
        out.append(rk.delay(15))
        for path in paths:
            for a, b in zip([last] + path[:-1], path):
                out += [_move(m, x, y, t) for x, y in _steps(a, b, spacing)]
            last = path[-1]
    return out[:block - 1] + [rk.instruction(rk.OP_END)]


def red_stop_block(m):
    """Captured: after stopping, park the mirrors at the centre and leave the red pointer on."""
    return [rk.mark_xy2(1, CENTRE, CENTRE, 1000), rk.instruction(rk.OP_RED_ON)]
