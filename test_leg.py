"""Replays a recorded worn-Joy-Con trace through LegTracker — no hardware.

The fixture leg_trace.csv was captured at ~66Hz with the left Joy-Con strapped
to the thigh, in three labeled phases: REST (0-8s), RUN (8-19s), SQUAT (19-30s).
We calibrate on the still REST window, replay every sample, and assert the
tracker counts the squats, flags the run window, and stays quiet at rest.
"""

import csv
import os
import unittest

from monitor import LegTracker

FIXTURE = os.path.join(os.path.dirname(__file__), "leg_trace.csv")


def load():
    rows = []
    with open(FIXTURE, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((
                float(r["t"]), r["phase"],
                (int(r["ax"]), int(r["ay"]), int(r["az"])),
                (int(r["gx"]), int(r["gy"]), int(r["gz"])),
            ))
    return rows


class LegTraceTests(unittest.TestCase):
    def setUp(self):
        self.rows = load()
        self.tk = LegTracker()
        # Calibrate on a clean still slice of the REST window (pre-fidget).
        for t, ph, a, g in self.rows:
            if 0.5 <= t <= 3.3:
                self.tk.calibrate(a)

    def replay(self):
        """Feed every sample; return per-phase state lists + reps at each
        phase boundary."""
        states = {"REST": [], "RUN": [], "SQUAT": []}
        reps_end = {}
        for t, ph, a, g in self.rows:
            states[ph].append(self.tk.update(a, g))
            reps_end[ph] = self.tk.squat_reps
        return states, reps_end

    def test_counts_squats(self):
        _, _ = self.replay()
        self.assertGreaterEqual(
            self.tk.squat_reps, 4,
            f"expected >=4 squats in the squat window, got {self.tk.squat_reps}")

    def test_no_squats_while_clearly_running(self):
        # The run->squat transition is fuzzy near the t=19 label boundary, so
        # check the UNAMBIGUOUSLY-running stretch (t 9-16): a real stride's tilt
        # is too brief to count, so no squats should accumulate there.
        reps_rest_end = reps_at_9 = reps_at_16 = None
        for t, ph, a, g in self.rows:
            self.tk.update(a, g)
            if ph == "REST":
                reps_rest_end = self.tk.squat_reps
            if reps_at_9 is None and t >= 9:
                reps_at_9 = self.tk.squat_reps
            if reps_at_16 is None and t >= 16:
                reps_at_16 = self.tk.squat_reps
        self.assertEqual(reps_rest_end, 0, "no squats should count during rest")
        self.assertEqual(
            reps_at_16, reps_at_9,
            f"steady running (t9-16) should count no squats, got {reps_at_16 - reps_at_9}")

    def test_run_window_flagged(self):
        states, _ = self.replay()
        run = states["RUN"]
        frac = sum(1 for s in run if s in ("run", "sprint")) / len(run)
        self.assertGreater(
            frac, 0.5, f"run window should read mostly run/sprint, got {frac:.2f}")

    def test_squat_window_mostly_squat(self):
        # The squat excursion should read 'squat' throughout, not flicker to
        # run/sprint as the leg swings between/within reps. 'squat' must be a
        # clear plurality and run/sprint a small minority over the SQUAT window.
        states, _ = self.replay()
        sq = states["SQUAT"]
        squat_frac = sum(1 for s in sq if s == "squat") / len(sq)
        runlike_frac = sum(1 for s in sq if s in ("run", "sprint")) / len(sq)
        self.assertGreater(
            squat_frac, 0.6,
            f"squat window should read mostly squat, got {squat_frac:.2f}")
        self.assertLess(
            runlike_frac, 0.2,
            f"squat window should rarely read run/sprint, got {runlike_frac:.2f}")

    def test_rest_tail_quiet(self):
        states, _ = self.replay()
        tail = states["REST"][-20:]
        self.assertTrue(all(s == "rest" for s in tail),
                        f"rest tail should be quiet, saw {sorted(set(tail))}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
