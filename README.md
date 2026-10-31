# RKutter

Linux control for **UV and fiber galvo laser markers driven by RK-CAD's network board**,
sent straight from Inkscape. A replacement for RK-CAD on Linux.

> ## ⚠️ Not yet tested on a laser
>
> RKutter has **not been run on a real board**. It has not been tested or debugged on hardware.
> Its jobs were checked against recordings of what RK-CAD sends, and matched them instruction
> for instruction, apart from a few points off by one. It has never driven a laser. Expect bugs.
>
> Known unknowns:
> - **Lens settings.** Until you copy RK-CAD's settings file over (see below), the field size and
>   lens correction are factory guesses. Marks may land slightly off size or position.
> - **Laser type and delays** are factory values until that same file is in place.
> - **The outline frame** (the red light tracing every outline) is built the same way as
>   RK-CAD's bounding-box frame, but it has not been compared against RK-CAD itself.
> - **Engraved shapes are marked outline first, then fill, one shape at a time.** This is
>   RK-CAD's "outline first" setting. Its exact order with several shapes has not been compared.
>
> Lasers injure eyes and can start fires. Wear the right eye protection for your wavelength,
> and stay with the machine.

RKutter is an independent project. It is not affiliated with or endorsed by the makers of
RK-CAD or the board. Product names belong to their owners.

## What it does

- **Inkscape menu** (Extensions → RKutter): send the drawing to the laser, set up a page the
  size of the lens field, and mark the selection as Cut, Score or Engrave.
- **The page is the lens field.** Its center is the lens center.
- **A color list like RK-CAD's.** Each color has its own power, speed, frequency, pulse width,
  passes, fill spacing, angle, cross-hatch and kerf. Any color can be switched off.
- **Frame** traces the bounding box, or every outline, with the red aiming light until Stop.
- **Simulate** plays the job on the drawing, with Stop, Clear and a speed slider. Fills play
  ten times faster than outlines, and a short job can play slower than real time.
- **Material test**: a labelled grid of squares, power down the rows and speed across.
- **Batch serial numbers**: put `#` signs in a text where the number goes (`####`, or `BK###`
  for BK001, BK002…). Batch numbers it from a first value for a number of pieces. After each
  piece the drawing shows the next number and waits for Next (or Enter). The count is
  remembered between sessions.
- **Telemetry** of everything sent and received, with a command line for raw board commands.

## Install

Needs Linux, Python 3.11 or newer, GTK 4 with its Python bindings (PyGObject and pycairo,
from your system's packages), and Inkscape 1.2 or newer. Plug the board into a wired network
port.

```
git clone https://github.com/Xeno-va/RKutter.git
cd RKutter
./install.sh
```

The board is reached with raw network frames, which needs one permission, so the installer
asks for your password once. The permission goes to a separate copy of Python, kept inside
the program's folder, that only opens the connection and hands it over. The panel itself
runs with ordinary rights. Run `./install.sh` again after a system Python update.

Then set `interface` in `app/lasers.txt` to the wired port the board is plugged into
(`ip link` lists them), and restart Inkscape.

## Your lens settings

Copy `lasermarkparam.ini` from the PC that runs RK-CAD into `app/lasers/rk-uv/`. RKutter
reads the lens correction, scale, power curve and laser type from it, just as RK-CAD does.

## Settings files

- `app/lasers.txt`: the laser list (network port, field size, settings file).
- `app/defaults.txt`: what Cut, Score and Engrave look like in Inkscape, and their starting
  settings.
- `app/profiles/rk-uv/`: presets.

## License

MIT. See LICENSE. Provided as is, with no warranty: you run it on your own laser at your own risk.
