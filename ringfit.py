"""Combined Ring-Con (right) + leg-strap (left) live readout.

The RIGHT Joy-Con drives the Ring-Con strain gauge; the LEFT Joy-Con is
strapped to the thigh and classified by LegTracker (rest/run/sprint/squat +
squat reps). Each pad runs on its own reader thread, so one stalling or
sleeping doesn't block the other. Either pad may be absent — the readout
just shows whatever is connected.

Run from the repo root:  python ringfit.py
(Hold the leg still for ~3s on start so LegTracker can calibrate its rest pose.)
"""

import sys
import threading
import time

from monitor import JoyCon, find_joycon, parse_report, LegTracker


class RightReader(threading.Thread):
    """Right Joy-Con: full Ring-Con init, then stream the strain byte."""

    def __init__(self, info):
        super().__init__(daemon=True)
        self.info = info
        self.strain = None
        self.status = "connecting"
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        jc = JoyCon(self.info["path"])
        if not jc.init_ringcon():
            self.status = "Ring-Con init failed"
            jc.close()
            return
        self.status = "connected"
        try:
            while not self._stop.is_set():
                buf = jc.read(timeout_ms=50)
                rep = parse_report(buf) if buf else None
                if rep and rep["strain"] is not None:
                    self.strain = rep["strain"]
        finally:
            jc.close()


class LeftReader(threading.Thread):
    """Left Joy-Con: IMU-only init, calibrate the rest pose for `calib_s`
    seconds, then feed samples to the shared LegTracker."""

    def __init__(self, info, tracker, calib_s=3.0):
        super().__init__(daemon=True)
        self.info = info
        self.tk = tracker
        self.calib_s = calib_s
        self.status = "connecting"
        self.calibrating = True
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        jc = JoyCon(self.info["path"])
        jc.init_imu_only()
        self.status = "calibrating"
        t0 = time.time()
        try:
            while not self._stop.is_set():
                buf = jc.read(timeout_ms=50)
                rep = parse_report(buf) if buf else None
                if not rep or rep["accel"] is None:
                    continue
                if self.calibrating:
                    self.tk.calibrate(rep["accel"])
                    if time.time() - t0 >= self.calib_s:
                        self.calibrating = False
                        self.status = "connected"
                elif rep["gyro"] is not None:
                    self.tk.update(rep["accel"], rep["gyro"])
        finally:
            jc.close()


def strain_bar(v, lo=0, mid=10, hi=20, width=16):
    if v is None:
        return "-" * (width + 2)
    pos = max(0, min(width, round((v - lo) / (hi - lo) * width)))
    return "[" + "#" * pos + "-" * (width - pos) + "]"


def main():
    right = find_joycon(side="R")
    left = find_joycon(side="L")
    print(f"right (Ring-Con): {'found' if right else 'NOT FOUND'}    "
          f"left (leg): {'found' if left else 'NOT FOUND'}")
    if not right and not left:
        print("No Joy-Cons found. Pair at least one over Bluetooth first.",
              file=sys.stderr)
        sys.exit(1)

    rr = RightReader(right) if right else None
    tk = LegTracker()
    lr = LeftReader(left, tk) if left else None
    for t in (rr, lr):
        if t:
            t.start()

    print("Streaming. Ctrl-C to stop.  (hold the leg still ~3s to calibrate)\n")
    try:
        while True:
            time.sleep(0.2)
            parts = []
            if rr:
                s = f"{rr.strain:2d}" if rr.strain is not None else "--"
                parts.append(f"RING strain={s} {strain_bar(rr.strain)} [{rr.status}]")
            if lr:
                tag = "calibrating" if lr.calibrating else lr.status
                parts.append(f"LEG {tk.state:6s} squats={tk.squat_reps:2d} "
                             f"tilt={tk.tilt:3.0f} [{tag}]")
            print("   ".join(parts) + "      \r", end="", flush=True)
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        for t in (rr, lr):
            if t:
                t.stop()


if __name__ == "__main__":
    main()
