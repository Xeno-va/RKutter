"""One connection class per kind of laser, all speaking the same few verbs.

Every class reports what it does through `tell(kind, text)`: "tx" for what
was sent, "rx" for what came back, "status" for decoded laser status,
"info" and "error". The panel shows these live and writes them to the log.
"""
import math
import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))



class Backend:
    def __init__(self, name, cfg, tell):
        self.name, self.cfg, self.tell = name, cfg, tell
        self.connected = False

    # Each verb runs on a worker thread; the panel never waits on it.
    def connect(self): raise NotImplementedError
    def run(self, job): raise NotImplementedError
    def pause(self): self.tell("info", "pause is not available on this laser yet")
    def resume(self): self.tell("info", "resume is not available on this laser yet")
    def stop(self): self.tell("info", "stop is not available on this laser yet")
    def test_fire(self, power_pct, ms): self.tell("info", "test fire is not available on this laser yet")
    def frame(self, paths): self.tell("info", "frame is not available on this laser yet")
    def home(self): self.tell("info", "home is not available on this laser yet")
    def console(self, text): self.tell("info", "typed commands are not available on this laser yet")
    def close(self): pass

    # The simulation and the run share the same job building, so what plays is what is sent.
    def simulate(self, job):
        """The head's path through the whole job: (t, x, y, on, engraving, pitch, note), one sample
        per point, t in seconds from the start, x and y in page mm, on = the laser was on getting
        to this point, engraving = this stretch is part of an engraving or fill; pitch is the mm
        between engraving lines, so they can be drawn as wide as they really are."""
        raise NotImplementedError


class RkUv(Backend):
    """The UV marker. Only what is proven decoded: finding the board and the red light."""

    def connect(self):
        import rklaser
        self.rk = rklaser
        iface = str(self.cfg.get("interface", "eth0")).split()[0]
        self.tell("tx", f"probe on {iface}")
        self.board = rklaser.Board(iface)
        self.tell("rx", f"board type {self.board.probe()}")
        self.connected = True

    def machine(self, rkjob):
        """RK-CAD's own settings file from the laser PC when it has been copied in, else factory values."""
        ini = str(self.cfg.get("settings", "")).split("#")[0].strip()
        path = Path(ini).expanduser() if ini else None
        if path is not None and not path.is_absolute():
            path = HERE / path  # relative to the program's folder
        if path is not None and path.exists():
            return rkjob.machine_from_ini(path)
        return rkjob.Machine(field_mm=float(str(self.cfg.get("field", "110")).split()[0]))

    def run(self, job):
        """Build the job the way RK-CAD does and send it. UNVERIFIED on the laser: the laser type, field
        size and delays are factory guesses until RK-CAD's settings are copied off the laser PC."""
        import rkjob
        m = self.machine(rkjob)
        page = job[0].get("page", (0, 0)) if job else (0, 0)
        self.page = page
        cx, cy = page[0] / 2, page[1] / 2  # page center goes to the lens center
        # Captured: RK-CAD asks where the mirrors are, and the first jump starts there.
        for _ in range(5):
            self.board.command(self.rk.CMD_BEFORE_MARK)
        self.board.buffer_status()
        mx, my, _ = self.board.xy2_position()
        self.tell("rx", f"mirrors at {mx}, {my}")
        here, first, instrs = rkjob.from_mirror(m, mx, my), True, []
        for pen, paths in self.layers(rkjob, job, cx, cy):
            if first:
                instrs += rkjob.job_start(m, pen, here)
            instrs += rkjob.mark_paths(m, pen, paths, here, start_sent=first)
            here, first = paths[-1][-1], False
        instrs += rkjob.job_end(m)
        self.tell("info", f"{len(instrs)} instructions, {len(instrs) // (self.rk.BLOCK - 1) + 1} blocks")
        self.stopping = False
        if not self.board.send_job(instrs, rkjob.job_wrap_up(m), self.tell, lambda: self.stopping):
            self.tell("info", "stopped")

    def layers(self, rkjob, job, cx, cy, flags=False):
        """Each color as (pen, lens-mm paths): every filled shape outlined and then filled, shape
        by shape, then the color's lines with its kerf applied. With flags, also which paths are
        fill lines. Colors with nothing to mark are left out."""
        import jobtools
        out = []
        lens = lambda polys: [[(x - cx, cy - y) for x, y in poly] for poly in polys]  # y up, about the page center
        for layer in job:
            p = layer["profile"]
            pen = rkjob.Pen(power=p.get("power", 10), speed_mm_s=p.get("speed_mm_s", 1000),
                            frequency_khz=p.get("frequency_khz", 40), passes=p.get("passes", 1),
                            fill_spacing_mm=p.get("fill_spacing_mm", 0.05), fill_angle=p.get("fill_angle", 0),
                            pulse_width=int(p.get("pulse_width_ns", 10)), cross_fill=p.get("cross_fill") == "yes")
            paths, filling = [], []
            # The user's way (2026-10-31): each engraved shape gets its edge marked, then its fill,
            # then the next shape: RK-CAD's per-fill "also mark the outline" with "outline first"
            # on. The captured RK-CAD jobs had the outline on and after the fill.
            for group in jobtools.fill_groups(layer["fills"]):
                edge = [g if g[0] == g[-1] else g + [g[0]] for g in lens(group)]
                lines = rkjob.hatch(lens(group), pen.fill_spacing_mm, pen.fill_angle, cross=pen.cross_fill)
                paths += edge + lines
                filling += [False] * len(edge) + [True] * len(lines)
            outlines = jobtools.kerf_offset(layer["paths"], float(p.get("kerf", 0)))
            paths += lens(outlines)
            filling += [False] * len(outlines)
            if paths:
                out.append((pen, paths, filling) if flags else (pen, paths))
        return out

    def simulate(self, job):
        """Jumps and marks at each color's speeds, every pass, from the lens center and back.
        Rough: settle times and laser delays are left out."""
        import numpy as np
        import rkjob
        page = job[0]["page"] if job else (0, 0)
        cx, cy = page[0] / 2, page[1] / 2
        t, x, y, on, eng = [0.0], [cx], [cy], [False], [False]

        def go(px, py, burn, speed, filling):
            t.append(t[-1] + math.dist((x[-1], y[-1]), (px, py)) / speed)
            x.append(px), y.append(py), on.append(burn), eng.append(filling)
        jump = 4000.0
        for pen, paths, filling in self.layers(rkjob, job, cx, cy, flags=True):
            jump = pen.jump_speed_mm_s
            for _ in range(pen.passes):
                for path, fill in zip(paths, filling):
                    page_path = [(px + cx, cy - py) for px, py in path]
                    go(*page_path[0], False, pen.jump_speed_mm_s, fill)
                    for pt in page_path[1:]:
                        go(*pt, True, pen.speed_mm_s, fill)
        go(cx, cy, False, jump, False)
        spacings = [float(l["profile"].get("fill_spacing_mm", 0.05)) for l in job if l["fills"]]
        return np.array(t), np.array(x), np.array(y), np.array(on), np.array(eng), \
            (spacings[0] if spacings else 0.0), ""

    def console(self, text):
        """A raw board command: its code and any argument bytes, in hex ("58", or "05 00 01").
        The reply frame shows in full."""
        parts = bytes.fromhex(text.replace("0x", "").replace(",", " "))
        if not parts:
            return
        self.tell("tx", f"command {parts[0]:#04x}" + (f" args {parts[1:].hex(' ')}" if parts[1:] else ""))
        self.tell("rx", self.board.command(parts[0], parts[1:]).hex(" "))

    def frame(self, paths):
        """RK-CAD's continuous red-light frame until Stop: the paths given (page mm), one box for
        the bounding box or every outline of the design."""
        import rkjob
        m = self.machine(rkjob)
        page = self.page
        cx, cy = page[0] / 2, page[1] / 2  # page center goes to the lens center, y flipped
        lens = [[(x - cx, cy - y) for x, y in p] for p in paths]
        self.framing = True
        self.tell("tx", f"red frame, {len(lens)} outline{'s' * (len(lens) != 1)} (continuous)")
        while self.framing:
            self.board.command(self.rk.CMD_START_BUFFER)
            self.board.send_batch(rkjob.red_frame_paths(m, lens)[:-1])
            self.board.command(self.rk.CMD_EXEC_BUFFER)

    def stop(self):
        """Captured from RK-CAD: during a mark the closing batch also turns the laser off; during
        the red frame it only parks the mirrors and leaves the red pointer on."""
        import rkjob
        m = self.machine(rkjob)
        framing, self.framing, self.stopping = getattr(self, "framing", False), False, True
        self.tell("tx", "stop: " + ("" if framing else "laser off, ") + "mirrors to center, red pointer on")
        self.board.stop(rkjob.red_stop_block(m) if framing else rkjob.job_wrap_up(m))

    def close(self):
        try:
            self.board.close()
        except Exception:
            pass


KINDS = {"rk-uv": RkUv}


def sweeps(layer):
    """The fill-line angle for an engraving swept up and down (90) or diagonally (45); None for
    left and right, which RetinaEngrave's own engrave job does."""
    if layer["profile"].get("mode") != "engrave":
        return None
    return {"up and down": 90.0, "diagonal": 45.0}.get(layer["profile"].get("direction"))


def fmt_time(seconds):
    seconds = round(seconds)
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def rasterize(fills, dpi, with_corner=False):
    """Filled areas (mm polylines) to 1-bit rows, 8 pixels per byte, even-odd fill; with_corner
    also returns the image's top-left corner in mm."""
    from PIL import Image, ImageChops, ImageDraw
    pts = [p for poly in fills for p in poly]
    if not pts:
        return ([], (0.0, 0.0)) if with_corner else []
    px = dpi / 25.4
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    w = int((max(p[0] for p in pts) - x0) * px) + 2
    h = int((max(p[1] for p in pts) - y0) * px) + 2
    img = Image.new("1", (w, h), 0)
    for poly in fills:
        layer = Image.new("1", (w, h), 0)
        ImageDraw.Draw(layer).polygon([((x - x0) * px, (y - y0) * px) for x, y in poly], fill=1)
        img = ImageChops.logical_xor(img, layer)
    rows = [img.crop((0, r, w, r + 1)).tobytes() for r in range(h)]
    return (rows, (x0, y0)) if with_corner else rows
