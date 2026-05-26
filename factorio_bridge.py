"""Drive the Ring Fit Miners Factorio mod from the leg-strap Joy-Con.

Reads the worn (preferably left) Joy-Con via monitor.LegTracker and toggles a
flag in a running Factorio game over RCON. The companion mod
(factorio-mod/ring-fit-miners) deactivates every mining-drill while that flag
is false, so miners only work while you're running in place.

Setup
-----
1. Copy ``factorio-mod/ring-fit-miners_0.1.0`` into your Factorio ``mods/``
   folder (``%APPDATA%/Factorio/mods`` on Windows, ``~/Library/Application
   Support/factorio/mods`` on macOS, ``~/.factorio/mods`` on Linux).
2. Enable RCON. For a single-player session, add to ``config/config.ini``::

       [other]
       rcon-port=27015
       rcon-password=secret

   Or for a headless server, launch with ``--rcon-port 27015 --rcon-password
   secret``. RCON listens on localhost only by default.
3. Strap the LEFT Joy-Con to your thigh, hold still for ~2s to calibrate,
   then start running. Miners come alive only while you do.

The protocol below is Source RCON (Valve), which Factorio implements verbatim.
Implemented inline here so the project stays pip-only with no new deps.
"""

import argparse
import os
import socket
import struct
import sys
import time

from monitor import (
    JoyCon, LegTracker, find_joycon, parse_report,
    JOYCON_L_PID, JOYCON_R_PID,
)


SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_AUTH_RESPONSE = 2


class RconError(Exception):
    pass


class Rcon:
    """Tiny Source RCON client — auth + single-shot exec is all we need."""

    def __init__(self, host, port, password, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._id = 0
        self._auth(password)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, type_, body):
        self._id += 1
        payload = struct.pack("<ii", self._id, type_) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._id

    def _readn(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RconError("rcon connection closed")
            out += chunk
        return out

    def _recv(self):
        (size,) = struct.unpack("<i", self._readn(4))
        body = self._readn(size)
        rid, type_ = struct.unpack_from("<ii", body, 0)
        # Body is null-terminated; the trailing two bytes are nulls.
        return rid, type_, body[8:-2].decode("utf-8", errors="replace")

    def _auth(self, password):
        self._send(SERVERDATA_AUTH, password)
        # Factorio replies with an empty SERVERDATA_RESPONSE_VALUE then the
        # SERVERDATA_AUTH_RESPONSE. Drain until we see the latter.
        for _ in range(4):
            rid, type_, _ = self._recv()
            if type_ == SERVERDATA_AUTH_RESPONSE:
                if rid == -1:
                    raise RconError("rcon auth rejected (wrong password?)")
                return
        raise RconError("rcon auth: no response")

    def cmd(self, line):
        self._send(SERVERDATA_EXECCOMMAND, line)
        _, _, body = self._recv()
        return body


def calibrate(jc, tracker, seconds):
    print(f"Hold still (strapped on) for {seconds:.1f}s to calibrate rest pose...")
    deadline = time.time() + seconds
    samples = 0
    while time.time() < deadline:
        buf = jc.read(timeout_ms=200)
        if not buf:
            continue
        rep = parse_report(buf)
        if rep and rep["accel"]:
            tracker.calibrate(rep["accel"])
            samples += 1
    if samples == 0:
        raise RuntimeError("no IMU samples during calibration — is the Joy-Con awake?")
    print(f"Calibrated on {samples} samples. rest_accel={tracker.rest_accel}")


def pick_joycon(prefer="L"):
    info = find_joycon(side=prefer)
    if info:
        return info, prefer
    other = "R" if prefer == "L" else "L"
    info = find_joycon(side=other)
    if info:
        print(f"Note: only the {other} Joy-Con is paired; using it for leg tracking.")
        return info, other
    return None, None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27015)
    ap.add_argument("--password", default=os.environ.get("FACTORIO_RCON_PASSWORD", ""))
    ap.add_argument("--calibrate-seconds", type=float, default=2.0)
    ap.add_argument("--side", choices=("L", "R"), default="L",
                    help="which Joy-Con to use as the leg strap (default: L)")
    args = ap.parse_args()

    if not args.password:
        print("RCON password missing. Pass --password or set FACTORIO_RCON_PASSWORD.",
              file=sys.stderr)
        sys.exit(2)

    info, side = pick_joycon(prefer=args.side)
    if not info:
        print("No Joy-Con found. Pair via System Settings -> Bluetooth first.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Using Joy-Con ({side}) at {info['path']!r} for leg tracking.")

    print(f"Connecting RCON {args.host}:{args.port}...")
    rcon = Rcon(args.host, args.port, args.password)
    print("RCON authenticated.")

    jc = JoyCon(info["path"])
    try:
        jc.init_imu_only()
        tracker = LegTracker()
        calibrate(jc, tracker, args.calibrate_seconds)

        # Force a known starting state in the game so we don't desync if the
        # mod was left in the opposite state by a previous session.
        rcon.cmd("/ring_fit_running 0")
        last_running = False
        print("Streaming. Run in place to keep your miners alive. Ctrl-C to stop.\n")

        while True:
            buf = jc.read(timeout_ms=200)
            if not buf:
                continue
            rep = parse_report(buf)
            if rep is None or rep["accel"] is None or rep["gyro"] is None:
                continue
            state = tracker.update(rep["accel"], rep["gyro"])
            running = state in ("run", "sprint")
            if running != last_running:
                last_running = running
                rcon.cmd(f"/ring_fit_running {1 if running else 0}")
                print(f"-> miners {'ON ' if running else 'OFF'}  "
                      f"state={state:<6} energy={tracker.energy:6.0f} tilt={tracker.tilt:5.1f}deg")
    except KeyboardInterrupt:
        print("\nstopping.")
        try:
            rcon.cmd("/ring_fit_running 0")
        except (RconError, OSError):
            pass
    finally:
        jc.close()
        rcon.close()


if __name__ == "__main__":
    main()
