"""Talk to an RK laser card (RKQ-LM-44x) directly over raw Ethernet.

Built from the protocol decoded out of RK-CAD's own driver (see
../PROTOCOL.md). Linux only, plain Python, no dependencies, so it runs the
same on a laptop or a Raspberry Pi. Sending needs raw network access: run as
root, or give the interpreter the CAP_NET_RAW capability.

Nothing here has been checked against a real board yet.
"""
import argparse
import socket
import struct
import sys
import threading
import time

BOARD_MAC = bytes.fromhex("080206031316")
HOST_MAC = b"\xff" * 6
ETHERTYPE = 0x88A4
FRAME_LEN = 0x5E6  # every frame, both directions, is this long
INSTR_LEN = 12
INSTR_PER_FRAME = (0x5D0 + INSTR_LEN - 1) // INSTR_LEN  # 124, as the driver packs them

TYPE_COMMAND = 2  # one-off command, code at byte 0x16
TYPE_BATCH = 3  # job instructions packed from byte 0x16

# Job instructions (type 3), first two bytes of each 12-byte instruction.
OP_DELAY = 0x01
OP_MARK_XY2 = 0x02
OP_RED_ON = 0x03
OP_RED_OFF = 0x04
OP_LASER_ENABLE = 0x05
OP_LASER_DISABLE = 0x06
OP_MO_ON = 0x07
OP_MO_OFF = 0x08
OP_FPK_HIGH = 0x09
OP_LASER_POWER = 0x0B
OP_POWER_LOCK_HIGH = 0x0C
OP_POWER_LOCK_LOW = 0x0D
OP_PA_HIGH = 0x0E
OP_PULSE_WIDTH = 0x10
OP_PWM_CYCLE_TIME = 0x14
OP_END = 0x17

# One-off commands (type 2), byte 0x16.
CMD_ALARM = 0x01
CMD_XY2_STATE = 0x02
CMD_XY2_POSITION = 0x03
CMD_BUFFER_STATUS = 0x04
CMD_START_BUFFER = 0x05
CMD_CLOSE_BUFFER = 0x06  # UNVERIFIED name: sent after every block but the first
CMD_EXEC_BUFFER = 0x07
CMD_STOP_BUFFER = 0x08
CMD_BEFORE_MARK = 0x0A  # UNVERIFIED meaning: RK-CAD sends it five times before every Mark
CMD_UNIQUE_ID = 0x0B
CMD_EXEC_BUFFER_LOOP = 0x0E
CMD_MARK_COST_TIME = 0x13
CMD_AXIS_STOP = 0x3D
CMD_INTERPOLATION_STOP = 0x4C
CMD_FW_VERSION = 0x58

BLOCK = 10000  # captured: RK-CAD streams a job in blocks of 9,999 instructions + END


def frame(seq, msg_type, body=b"", flag=0):
    """One full-length frame: header, frame number, flag, type, body, zero pad."""
    head = BOARD_MAC + HOST_MAC + struct.pack(">HHHHH", ETHERTYPE, seq, flag, 0, msg_type)
    out = head + body
    if len(out) > FRAME_LEN:
        raise ValueError(f"frame body too long: {len(out)} > {FRAME_LEN}")
    return out.ljust(FRAME_LEN, b"\0")


def probe_frame():
    """What the driver sends to find the board: header only, everything else zero."""
    return (BOARD_MAC + HOST_MAC + struct.pack(">H", ETHERTYPE)).ljust(FRAME_LEN, b"\0")


def checkin_frame():
    """The fixed frame the driver repeats while idle."""
    return frame(0, TYPE_BATCH, flag=0x1111)


def instruction(op, values=b""):
    """A 12-byte job instruction: code, then its values high byte first."""
    out = struct.pack(">H", op) + values
    if len(out) > INSTR_LEN:
        raise ValueError(f"instruction 0x{op:02x} values too long")
    return out.ljust(INSTR_LEN, b"\0")


def delay(t):
    return instruction(OP_DELAY, struct.pack(">I", t))


def mark_xy2(mode, x, y, z=0):
    # UNVERIFIED: what mode means and the scale of x and y.
    return instruction(OP_MARK_XY2, struct.pack(">Hhhh", mode, x, y, z))


def laser_power(p):
    return instruction(OP_LASER_POWER, struct.pack(">H", p))


def pulse_width(w):
    return instruction(OP_PULSE_WIDTH, struct.pack(">H", w))


def batch_frames(instructions, first_seq):
    """Split instructions into batch frames, with the closing instruction last."""
    items = list(instructions) + [instruction(OP_END)]
    frames, seq = [], first_seq
    for i in range(0, len(items), INSTR_PER_FRAME):
        frames.append((seq, frame(seq, TYPE_BATCH, b"".join(items[i:i + INSTR_PER_FRAME]))))
        seq = (seq + 1) & 0xFFFF
    return frames


def command_frame(seq, code, args=b""):
    return frame(seq, TYPE_COMMAND, bytes([code, 0]) + args)


def open_raw(iface):
    """The raw connection on one network interface: opened here when this program may, otherwise
    by the helper that holds the permission (see rawsock.py; install.sh sets it up), which hands it back."""
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        raw.bind((iface, 0))
        return raw
    except PermissionError:
        pass
    import subprocess
    from pathlib import Path
    helper_python = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python-laser"
    if not helper_python.exists():
        raise PermissionError("raw network not permitted, and no permitted Python copy to open it")
    ours, theirs = socket.socketpair()
    with ours, theirs:
        done = subprocess.run([str(helper_python), str(Path(__file__).with_name("rawsock.py")), iface,
                               hex(ETHERTYPE), str(theirs.fileno())], pass_fds=[theirs.fileno()],
                              capture_output=True, text=True, timeout=10)
        if done.returncode:
            raise PermissionError(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "helper failed")
        _msg, fds, _flags, _addr = socket.recv_fds(ours, 16, 1)
    return socket.socket(fileno=fds[0])


class Board:
    """A live link to the board on one network interface."""

    def __init__(self, iface, timeout=0.5):
        self.sock = open_raw(iface)
        self.sock.settimeout(timeout)
        self.seq = 0
        # RK-CAD sends a check-in about every 100 ms while connected (captured); do the same,
        # but never in the middle of an exchange.
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._checkins, daemon=True).start()

    def _checkins(self):
        frame_ = checkin_frame()
        while self.alive:
            time.sleep(0.1)
            if self.lock.acquire(blocking=False):
                try:
                    self.sock.send(frame_)
                except OSError:
                    pass
                finally:
                    self.lock.release()

    def close(self):
        self.alive = False
        with self.lock:
            self.sock.close()

    def _next_seq(self):
        self.seq = (self.seq + 1) & 0xFFFF or 1
        return self.seq

    def recv(self):
        """Next frame from the board, or None on timeout. Frames from anyone else are dropped."""
        deadline = time.monotonic() + self.sock.gettimeout()
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                return None
            if data[6:12] == BOARD_MAC:
                return data
        return None

    def exchange(self, out, want_seq, tries=20):
        """Send a frame until a reply with the same frame number comes back."""
        with self.lock:
            return self._exchange(out, want_seq, tries)

    def _exchange(self, out, want_seq, tries):
        for _ in range(tries):
            self.sock.send(out)
            reply = self.recv()
            while reply is not None:
                if struct.unpack(">H", reply[0x0E:0x10])[0] == want_seq:
                    return reply
                reply = self.recv()
        raise TimeoutError(f"no reply for frame {want_seq}")

    def probe(self):
        """Ask the board what it is. Returns the board type."""
        reply = self.exchange(probe_frame(), 0)
        return struct.unpack(">H", reply[0x12:0x14])[0]

    def command(self, code, args=b""):
        seq = self._next_seq()
        return self.exchange(command_frame(seq, code, args), seq)

    def send_batch(self, instructions):
        for seq, out in batch_frames(instructions, self._next_seq()):
            self.exchange(out, seq, tries=100)
            self.seq = seq

    # Reply layouts from RKLib's packet handler (0x180056d70), values high byte first.
    def xy2_position(self):
        """Where the mirrors are: three 16-bit values at 0x18, 0x1A, 0x1C (x, y, z)."""
        r = self.command(CMD_XY2_POSITION)
        return struct.unpack(">HHH", r[0x18:0x1E])

    def buffer_status(self):
        """Two 16-bit values at 0x18 and 0x1C; RKLib calls the buffer free when both are 0."""
        r = self.command(CMD_BUFFER_STATUS)
        return struct.unpack(">H", r[0x18:0x1A])[0], struct.unpack(">H", r[0x1C:0x1E])[0]

    def wait_buffer_free(self, timeout=None, every=0.02):
        """Captured: after the last block RK-CAD asks for buffer status about every 20 ms until the
        board answers 0, 0 (RKLib's isBufferFree)."""
        end = None if timeout is None else time.monotonic() + timeout
        while self.buffer_status() != (0, 0):
            if end is not None and time.monotonic() > end:
                raise TimeoutError("board buffer never came free")
            time.sleep(every)

    def mark_cost_time(self):
        """16-bit value at 0x20 x 1000 + 16-bit value at 0x1E."""
        r = self.command(CMD_MARK_COST_TIME)
        return struct.unpack(">H", r[0x20:0x22])[0] * 1000 + struct.unpack(">H", r[0x1E:0x20])[0]

    def wrap_up(self, batch):
        """Captured, the same after a finished job and after Stop: start buffer, interpolation stop,
        axis stop, the closing batch (laser off, mirrors to centre, red pointer on), run."""
        self.command(CMD_START_BUFFER, b"\x00\x01")
        self.command(CMD_INTERPOLATION_STOP)
        self.command(CMD_AXIS_STOP)
        self.send_batch(batch)
        self.command(CMD_EXEC_BUFFER, b"\x00\x01")

    def send_job(self, instructions, wrap_up, tell=lambda *a: None, stopped=lambda: False):
        """A whole job, framed exactly as RK-CAD frames every Mark (captured 2026-10-30).
        `instructions` and `wrap_up` must not contain END. Later blocks are not held back: RK-CAD
        sends each straight away, starting it as 2 (add to the running job) when the board reports
        busy and 1 when idle; the board paces it by when it answers. `stopped` is checked between
        blocks and while waiting; when it turns true, call stop() instead of finishing."""
        blocks = [instructions[i:i + BLOCK - 1] for i in range(0, len(instructions), BLOCK - 1)] or [[]]
        self.command(CMD_STOP_BUFFER)
        self.buffer_status()
        self.command(CMD_START_BUFFER, b"\x00\x01")
        tell("tx", f"block 1 of {len(blocks)}")
        self.send_batch(blocks[0])
        self.command(CMD_EXEC_BUFFER, b"\x00\x01")
        for k, block in enumerate(blocks[1:], 2):
            if stopped():
                return False
            busy = self.buffer_status() != (0, 0)
            self.command(CMD_START_BUFFER, b"\x00\x02" if busy else b"\x00\x01")
            tell("tx", f"block {k} of {len(blocks)}")
            self.send_batch(block)
            self.command(CMD_CLOSE_BUFFER)
        while self.buffer_status() != (0, 0):
            if stopped():
                return False
            time.sleep(0.02)
        tell("rx", f"mark cost time {self.mark_cost_time()}")
        self.command(CMD_STOP_BUFFER)
        self.buffer_status()
        self.wrap_up(wrap_up)
        return True

    def stop(self, wrap_up):
        """Captured Stop during a mark: stop buffer twice, buffer status, then the wrap-up."""
        self.command(CMD_STOP_BUFFER)
        self.command(CMD_STOP_BUFFER)
        self.buffer_status()
        self.wrap_up(wrap_up)


def hexdump(data, width=16):
    last = len(data.rstrip(b"\0")) or 1
    shown = data[:((last + width - 1) // width) * width]
    for i in range(0, len(shown), width):
        print(f"{i:04x}  {shown[i:i + width].hex(' ')}")
    if len(shown) < len(data):
        print(f"      ... zero to {len(data)} bytes")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iface", help="network interface cabled to the laser")
    ap.add_argument("--dry-run", action="store_true", help="print the frames instead of sending")
    sub = ap.add_subparsers(dest="what", required=True)
    sub.add_parser("probe", help="find the board and report its type")
    red = sub.add_parser("red", help="red aiming light on or off; cannot fire the marking laser")
    red.add_argument("state", choices=["on", "off"])
    sub.add_parser("version", help="read the firmware version")
    args = ap.parse_args(argv)

    if args.what == "probe":
        frames = [("probe", probe_frame())]
    elif args.what == "red":
        op = OP_RED_ON if args.state == "on" else OP_RED_OFF
        frames = [("start buffer", command_frame(1, CMD_START_BUFFER))]
        frames += [(f"batch frame {s}", f) for s, f in batch_frames([instruction(op)], 2)]
        frames.append(("run buffer", command_frame(3, CMD_EXEC_BUFFER)))
    else:
        frames = [("firmware version", command_frame(1, CMD_FW_VERSION))]

    if args.dry_run:
        for label, f in frames:
            print(f"-- {label}")
            hexdump(f)
        return 0

    if not args.iface:
        ap.error("--iface is required unless --dry-run")
    board = Board(args.iface)
    try:
        kind = board.probe()
        print(f"board answered, type {kind}")
        if args.what == "red":
            board.command(CMD_START_BUFFER)
            board.send_batch([instruction(op)])
            board.command(CMD_EXEC_BUFFER)
            print(f"red light {args.state}")
        elif args.what == "version":
            reply = board.command(CMD_FW_VERSION)
            hexdump(reply)
    finally:
        board.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
