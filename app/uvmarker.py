"""The UV marker panel (galvo, fixed head): a list of the drawing's colors, each with its own
settings, a Presets tab, and Frame / Start / Stop. The page is the lens field and its center
is the lens center.

usage: uvmarker.py [--laser NAME] [drawing.svg]
"""
import argparse

from window import (Gtk, LaserWindow, big_button, pick, preset_picker, presets_tab, replace_page, run_app,
                    save_preset_button, settings, spin, spin_value)

KIND = "rk-uv"

# One set of settings per color. ("key", label, low, high, step, digits, default)
VECTOR = [("power", "Power %", 0, 100, 1, 0, 50), ("speed_mm_s", "Speed mm/s", 1, 10000, 10, 0, 1000),
          ("frequency_khz", "Frequency kHz", 1, 300, 1, 0, 30), ("pulse_width_ns", "Pulse width ns", 1, 500, 1, 0, 10),
          ("passes", "Passes", 1, 20, 1, 0, 1)]
FILL = [("fill_spacing_mm", "Line spacing mm", 0.01, 5, 0.01, 2, 0.05), ("fill_angle", "Angle", 0, 179, 1, 0, 0)]
CUT = [("kerf", "Kerf mm", 0, 2, 0.01, 2, 0)]
CROSS = ["no", "yes"]
FRAME_SHAPES = ["bounding box", "outline"]
KEYS = [f[0] for f in VECTOR + FILL + CUT] + ["cross_fill"]


def contents(kinds):
    n, f = len(kinds["stroke"]), len(kinds["fill"])
    return ", ".join(x for x in (f"{n} outline{'s' * (n != 1)}" if n else "", f"{f} filled" if f else "") if x)


def first_laser(kind):
    return next(n for n, c in settings.lasers().items() if c.get("kind") == kind)


class UvMarkerPanel(LaserWindow):
    def bed_note(self):
        return "page = lens field; its center is the lens center"

    def draw_marks(self, cr, s):
        # The lens center, where the red pointer rests.
        cx, cy = self.page[0] * s / 2, self.page[1] * s / 2
        cr.set_source_rgb(0.85, 0.2, 0.2)
        cr.set_line_width(1)
        for dx, dy in ((6, 0), (0, 6)):
            cr.move_to(cx - dx, cy - dy)
            cr.line_to(cx + dx, cy + dy)
        cr.stroke()

    # ------------------------------------------------------------ settings
    def build_settings(self):
        saved = settings.remembered(self.drawing_key())
        starts = settings.color_defaults(KIND)
        self.pens = {}
        for color in self.layers:
            start = starts.get(color.lower(), {})
            pen = {k: saved.get(f"{color}.{k}", start.get(k, d)) for k, *_r, d in VECTOR + FILL + CUT}
            pen["cross_fill"] = saved.get(f"{color}.cross_fill", start.get("cross_fill", "no"))
            pen["use"] = saved.get(f"{color}.use", saved.get(f"{color}.mark", "yes"))
            self.pens[color] = pen
        self.current = None
        self.tabs = Gtk.Notebook()
        self.tabs.append_page(self.colors_tab(), Gtk.Label(label="Color Mapping"))
        self.tabs.append_page(self.presets(), Gtk.Label(label="Presets"))
        if self.layers:
            self.color_list.select_row(self.color_list.get_row_at_index(0))
        return self.tabs

    def refresh(self):
        keep = self.current
        replace_page(self.tabs, 0, self.colors_tab(), "Color Mapping")
        replace_page(self.tabs, 1, self.presets(), "Presets")
        if keep:
            self.color_list.select_row(self.color_list.get_row_at_index(list(self.layers).index(keep)))

    def colors_tab(self):
        """Left: the colors, each ticked to run or not. Right: the selected color's settings."""
        outer = Gtk.Box(spacing=12, margin_top=6, margin_start=6, margin_end=6, margin_bottom=6)
        self.color_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        for color, kinds in self.layers.items():
            row = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
            check = Gtk.CheckButton(active=self.pens[color]["use"] == "yes", tooltip_text="run this color")
            check.connect("toggled", lambda w, c=color: self.set_pen(c, "use", "yes" if w.get_active() else "no"))
            swatch = Gtk.Label()
            swatch.set_markup(f'<span background="{color}">     </span>')
            for w in (check, swatch, Gtk.Label(label=color), Gtk.Label(label=contents(kinds), css_classes=["dim-label"])):
                row.append(w)
            self.color_list.append(row)
        self.color_list.connect("row-selected", lambda _l, row: row and self.show_pen(list(self.layers)[row.get_index()]))
        outer.append(Gtk.Frame(child=self.color_list, valign=Gtk.Align.START))

        self.form = {}
        # Vector setting on the left; Fill and Cut stacked on the right, so the form stays short.
        boxes, stack = Gtk.Box(spacing=8), Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for title, fields in (("Vector setting", VECTOR), ("Fill setting", FILL), ("Cut setting", CUT)):
            grid = Gtk.Grid(column_spacing=10, row_spacing=4, margin_top=4, margin_bottom=6, margin_start=8,
                            margin_end=8)
            for r, (key, label, lo, hi, step, digits, default) in enumerate(fields):
                grid.attach(Gtk.Label(label=label, xalign=0, hexpand=True), 0, r, 1, 1)
                w = spin(lo, hi, step, digits, default)
                w.connect("value-changed", lambda w, k=key: self.current and self.set_pen(self.current, k, spin_value(w)))
                grid.attach(w, 1, r, 1, 1)
                self.form[key] = w
            if fields is FILL:
                grid.attach(Gtk.Label(label="Cross-hatch", xalign=0), 0, len(fields), 1, 1)
                w = pick(CROSS, "no")
                w.connect("notify::selected", lambda w, *_: self.current and
                          self.set_pen(self.current, "cross_fill", CROSS[w.get_selected()]))
                grid.attach(w, 1, len(fields), 1, 1)
                self.form["cross_fill"] = w
                self.fill_grid = grid
            label = Gtk.Label()
            label.set_markup(f"<b>{title}</b>")
            (boxes if fields is VECTOR else stack).append(Gtk.Frame(label_widget=label, child=grid, valign=Gtk.Align.START))
        boxes.append(stack)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        # The color's name and its preset load and save share one line, so the form stays short.
        title_row = Gtk.Box(spacing=8)
        self.pen_title = Gtk.Label(xalign=0)
        title_row.append(self.pen_title)
        title_row.append(preset_picker(KIND, lambda v, n: self.current and self.load_pen(self.current, v, n)))
        title_row.append(save_preset_button(self, KIND, lambda: {k: self.pens[self.current][k] for k in KEYS} if self.current else {},
                                            self.refresh))
        right.append(title_row)
        right.append(boxes)
        outer.append(right)
        return outer

    def show_pen(self, color):
        self.current = None  # hold off saving while the form is refilled
        pen = self.pens[color]
        for key, w in self.form.items():
            if key == "cross_fill":
                w.set_selected(CROSS.index(str(pen[key])) if str(pen[key]) in CROSS else 0)
            else:
                w.set_value(float(pen[key]))
        # Fill settings mean nothing on a color that is outlines only.
        self.fill_grid.set_sensitive(bool(self.layers[color]["fill"]))
        self.pen_title.set_markup(f'<span background="{color}">     </span>  <b>{color}</b>')
        self.current = color

    def remembered_pens(self):
        return {f"{c}.{k}": v for c, pen in self.pens.items() for k, v in pen.items()}

    def set_pen(self, color, key, value):
        self.pens[color][key] = value
        shape = FRAME_SHAPES[self.frame_shape.get_selected()] if hasattr(self, "frame_shape") else "bounding box"
        settings.remember(self.drawing_key(), dict(self.remembered_pens(), frame_shape=shape))

    def load_pen(self, color, values, name):
        for key in KEYS:
            if key in values:
                self.set_pen(color, key, values[key])
        self.show_pen(color)
        self.tell("info", f"{color}: loaded preset {name}")

    def presets(self):
        return presets_tab(self, KIND, [("Power %", lambda v: v.get("power", "—")),
                                        ("Speed mm/s", lambda v: v.get("speed_mm_s", "—")),
                                        ("Frequency kHz", lambda v: v.get("frequency_khz", "—")),
                                        ("Pulse width ns", lambda v: v.get("pulse_width_ns", "—")),
                                        ("Line spacing mm", lambda v: v.get("fill_spacing_mm", "—"))],
                           "Load into selected color", lambda v, n: self.current and self.load_pen(self.current, v, n),
                           "Save selected color as preset…",
                           lambda: {k: self.pens[self.current][k] for k in KEYS} if self.current else {}, self.refresh)

    # ------------------------------------------------------------ buttons
    def build_buttons(self):
        row = Gtk.Box(spacing=8)
        self.frame_shape = pick(FRAME_SHAPES, settings.remembered(self.drawing_key()).get("frame_shape", "bounding box"))
        self.frame_shape.set_tooltip_text("what Frame traces")
        self.frame_shape.connect("notify::selected", lambda w, *_: settings.remember(
            self.drawing_key(), dict(self.remembered_pens(), frame_shape=FRAME_SHAPES[w.get_selected()])))
        row.append(self.frame_shape)
        for b in (big_button("Frame", self.frame), big_button("Start", self.start, "suggested-action"),
                  big_button("Stop", lambda: self.work(self.backend.stop), "destructive-action")):
            b.set_hexpand(True)
            row.append(b)
        return [row]

    def frame(self):
        """RK-CAD's frame: the bounding box or every outline traced in the red aiming light until Stop."""
        self.backend.page = self.page
        paths = self.frame_paths(FRAME_SHAPES[self.frame_shape.get_selected()])
        self.work(lambda: self.backend.frame(paths))

    def preview_job(self):
        return [{"color": c, "profile": dict({k: pen[k] for k in KEYS}, mode="mark"),
                 "paths": self.layers[c]["stroke"], "fills": self.layers[c]["fill"], "page": self.page}
                for c, pen in self.pens.items() if pen["use"] == "yes"]

    def start_point(self):
        return (self.page[0] / 2, self.page[1] / 2)

    def console_hint(self):
        return "board command in hex, code then arguments (e.g. 58), Enter sends"

    # ------------------------------------------------------------ material test
    def test_settings(self):
        return {"speed_unit": "mm/s", "fill": "hatch",
                "fields": [("speed_lo", "Speed mm/s from", 10, 10000, 50, 0, 200),
                           ("speed_hi", "Speed mm/s to", 10, 10000, 50, 0, 2000),
                           ("frequency_khz", "Frequency kHz", 1, 300, 1, 0, 30),
                           ("pulse_width_ns", "Pulse width ns", 1, 500, 1, 0, 10),
                           ("fill_spacing_mm", "Hatch spacing mm", 0.01, 5, 0.01, 2, 0.05),
                           ("label_power", "Label power %", 0, 100, 1, 0, 30),
                           ("label_speed", "Label speed mm/s", 10, 10000, 50, 0, 1000)]}

    def run_test(self, test, bounds, frame_only):
        """The grid centered on the lens; the page is the lens field (its size from the laser list
        when no drawing is open)."""
        t, cells, labels = test
        page = self.page if self.page[0] else (float(str(self.cfg.get("field", "110")).split()[0]),) * 2
        dx = page[0] / 2 - (bounds[0] + bounds[2]) / 2
        dy = page[1] / 2 - (bounds[1] + bounds[3]) / 2
        move = lambda p: [(x + dx, y + dy) for x, y in p]
        self.backend.page = page
        if frame_only:
            box = move([(bounds[0], bounds[1]), (bounds[2], bounds[1]), (bounds[2], bounds[3]),
                        (bounds[0], bounds[3]), (bounds[0], bounds[1])])
            self.work(lambda: self.backend.frame([box]))
            return
        pen = {k: t[k] for k in ("frequency_khz", "pulse_width_ns", "fill_spacing_mm")}
        hatch = t["kind"] == "hatch"
        job = [{"color": f"{power:g}% {speed:g}mm/s", "page": page,
                "profile": dict(pen, mode="mark", power=power, speed_mm_s=speed, passes=1),
                "paths": [] if hatch else [move(sq)], "fills": [move(sq)] if hatch else []}
               for power, speed, sq in cells]
        job.append({"color": "labels", "page": page, "fills": [], "paths": [move(p) for p in labels],
                    "profile": dict(pen, mode="mark", power=t["label_power"], speed_mm_s=t["label_speed"], passes=1)})
        self.tell("info", f"material test: {len(cells)} squares ({t['kind']}) on the lens center")
        self.work(lambda: self.backend.run(job))

    def start(self):
        job = self.preview_job()
        if not job:
            self.tell("error", "nothing to run: every color is unticked")
            return
        self.tell("info", "start: " + ", ".join(l["color"] for l in job))
        self.work(lambda: self.backend.run(job))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laser")
    ap.add_argument("svg", nargs="?")
    args = ap.parse_args()
    name = args.laser or first_laser(KIND)
    run_app("rkutter.uvmarker", lambda app: UvMarkerPanel(app, name, args.svg).present())


if __name__ == "__main__":
    main()
