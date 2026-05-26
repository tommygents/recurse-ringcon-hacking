"""Ring-Con-driven Doom (built on StanislavPetrovV/DOOM-style-Game).

Controls (Ring-Con = right hand; leg strap = left Joy-Con)
  Squeeze (push) inward     → fire (held = auto-fire as it reloads)
  Pull (stretch apart) edge → cycle weapon
  Run in place (leg)        → move; tilt forward/back picks the direction
  Tilt left  / right        → turn / aim
  Squat (leg)               → freeze the aim for a stable shot

Difficulty tweaks: monster damage is halved and player weapon damage doubled.

On launch, hold the ring still for ~1.5s while the rest pose is calibrated.
The leg Joy-Con self-calibrates in parallel — stand still while it does.

The base game uses keyboard (WASD) + mouse (look + fire). We subclass Player
to override movement/mouse_control with Ring-Con-derived values, and patch the
class reference in main so the existing Game.new_game() picks it up. A
background JoyConReader thread tracks strain and accel at HID rate and
exposes simple boolean / continuous inputs.
"""

import math
import os
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DOOM_DIR = os.path.join(PROJECT_ROOT, "doom")
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, DOOM_DIR)
# The base game uses relative paths for sound/sprite resources.
os.chdir(DOOM_DIR)

import pygame as pg

# Patch settings BEFORE importing any doom module that does `from settings import *`.
# The default 1600x900 is sluggish for a pure-Python raycaster on macOS.
import settings as st
st.RES = st.WIDTH, st.HEIGHT = 960, 540
st.HALF_WIDTH = st.WIDTH // 2
st.HALF_HEIGHT = st.HEIGHT // 2
st.NUM_RAYS = st.WIDTH // 2
st.HALF_NUM_RAYS = st.NUM_RAYS // 2
st.DELTA_ANGLE = st.FOV / st.NUM_RAYS
st.SCREEN_DIST = st.HALF_WIDTH / math.tan(st.HALF_FOV)
st.SCALE = st.WIDTH // st.NUM_RAYS
st.MOUSE_BORDER_LEFT = 100
st.MOUSE_BORDER_RIGHT = st.WIDTH - st.MOUSE_BORDER_LEFT

import main as doom_main          # noqa: E402
from player import Player         # noqa: E402
from weapon import Weapon         # noqa: E402
from collections import deque     # noqa: E402

from monitor import JoyCon, find_joycon, parse_report, LegTracker  # noqa: E402


# ----- Ring-Con state + reader thread -----

ACCEL_TILT_SCALE = 4000.0  # raw accel delta corresponding to "full tilt"
TILT_DEADZONE = 0.12
PUSH_DELTA = 2             # strain ≥ rest + this → fire held
PULL_DELTA = 2             # strain ≤ rest - this → cycle trigger


class RingState:
    def __init__(self):
        self.strain = None
        self.accel = None
        self.rest_strain = None
        self.rest_accel = None
        # derived booleans
        self.fire_held = False
        self.cycle_pulse = False  # set on falling edge of pull
        # Axis layout — adjustable at runtime:
        #   P / Shift+P : cycle pitch axis / flip pitch sign
        #   R / Shift+R : cycle roll axis  / flip roll sign
        #   ,  /  .     : turn slower / faster
        self.pitch_axis = 2  # try Z first for forward/back lean
        self.roll_axis = 0
        self.pitch_sign = -1  # confirmed by user via Shift+P
        self.roll_sign = -1
        self.turn_rate = 2.2  # radians/sec at full roll


class RingReader(threading.Thread):
    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state
        self.status = "connecting"
        self._stop = threading.Event()
        self._pull_armed = True

    def stop(self):
        self._stop.set()

    def _update_strain_inputs(self):
        s, r = self.state.strain, self.state.rest_strain
        if s is None or r is None:
            return
        self.state.fire_held = s >= r + PUSH_DELTA
        if s <= r - PULL_DELTA and self._pull_armed:
            self.state.cycle_pulse = True
            self._pull_armed = False
        if s >= r - (PULL_DELTA - 1):
            self._pull_armed = True

    def run(self):
        info = find_joycon(side="R")   # Ring-Con is the RIGHT pad
        if not info:
            self.status = "no Joy-Con found"
            return
        jc = JoyCon(info["path"])
        if not jc.init_ringcon():
            self.status = "Ring-Con init failed"
            jc.close()
            return
        self.status = "connected"
        try:
            while not self._stop.is_set():
                buf = jc.read(timeout_ms=50)
                if not buf:
                    continue
                rep = parse_report(buf)
                if not rep:
                    continue
                if rep["strain"] is not None:
                    self.state.strain = rep["strain"]
                    self._update_strain_inputs()
                if rep["accel"] is not None:
                    self.state.accel = rep["accel"]
        finally:
            jc.close()


# ----- Leg Joy-Con reader: feeds the shared LegTracker -----

class LegReader(threading.Thread):
    """Left (leg-strap) Joy-Con on its own thread. IMU-only init, calibrate the
    rest pose for calib_s seconds, then feed samples to the LegTracker. Sets
    .connected once calibrated so the player can tell whether to gate on leg
    state (no leg pad -> don't gate, game plays the old way)."""

    def __init__(self, info, tracker, calib_s=3.0):
        super().__init__(daemon=True)
        self.info = info
        self.tk = tracker
        self.calib_s = calib_s
        self.connected = False
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        jc = JoyCon(self.info["path"])
        jc.init_imu_only()
        t0 = time.time()
        calibrating = True
        try:
            while not self._stop.is_set():
                buf = jc.read(timeout_ms=50)
                rep = parse_report(buf) if buf else None
                if not rep or rep["accel"] is None:
                    continue
                if calibrating:
                    self.tk.calibrate(rep["accel"])
                    if time.time() - t0 >= self.calib_s:
                        calibrating = False
                        self.connected = True
                elif rep["gyro"] is not None:
                    self.tk.update(rep["accel"], rep["gyro"])
        finally:
            jc.close()


# ----- Player subclass that consumes RingState -----

def make_ring_player_cls(state, leg, leg_reader):
    class RingPlayer(Player):
        def __init__(self, game):
            super().__init__(game)
            self.state = state
            self._aim_frozen = False

        def check_game_over(self):
            # Base implementation does pg.time.delay(1500) during the death
            # screen, which lets KEYDOWN/MOUSEBUTTON events pile up. If we
            # don't drain them, the player respawns and immediately processes
            # 1.5s of buffered input (often firing/cycling unexpectedly).
            #
            # Also: new_game() replaces game.raycasting with a fresh instance
            # whose objects_to_render is empty. The very next game.draw() in
            # the SAME frame then renders nothing for walls / sprites, leaving
            # one frame of sky + floor with no geometry. Prime the new
            # raycasting once so the upcoming draw has something to blit.
            if self.health < 1:
                self.game.object_renderer.game_over()
                pg.display.flip()
                pg.time.delay(1500)
                pg.event.clear()
                state.cycle_pulse = False
                self.game.new_game()
                # The 1.5s delay above would otherwise land in the next
                # clock.tick(FPS) as a ~1500ms delta_time; our leg-driven
                # movement multiplies speed by that and rockets the fresh
                # player through a wall (out of bounds). Absorb the delay now
                # so the next frame's delta_time is normal.
                self.game.clock.tick()
                self.game.raycasting.update()

        def _axis(self, axis, sign):
            a, r = self.state.accel, self.state.rest_accel
            if a is None or r is None:
                return 0.0
            v = sign * (a[axis] - r[axis]) / ACCEL_TILT_SCALE
            v = max(-1.0, min(1.0, v))
            return 0.0 if abs(v) < TILT_DEADZONE else v

        def _leg_running(self):
            # No leg pad connected -> don't gate (game plays the old way).
            if leg_reader is None or not leg_reader.connected:
                return True
            return leg.state in ("run", "sprint")

        def movement(self):
            # Cap delta_time so a frame hitch (the death-screen delay, an
            # alt-tab, a GC pause) can't turn one step into a wall-clipping leap.
            dt = min(self.game.delta_time, 50)
            speed = st.PLAYER_SPEED * dt
            pitch = self._axis(self.state.pitch_axis, self.state.pitch_sign)
            # Forward/back only while running in place; tilt picks direction.
            if self._leg_running():
                cos_a, sin_a = math.cos(self.angle), math.sin(self.angle)
                dx = pitch * speed * cos_a
                dy = pitch * speed * sin_a
                self.check_wall_collision(dx, dy)
            self.angle %= math.tau

        def mouse_control(self):
            # Squat freezes the aim for a stable shot. Engage on a real (latched)
            # squat, but RELEASE the instant you stand — keyed on instantaneous
            # tilt dropping, not the energy-latched leg.state, which lingers
            # while the stand-up motion keeps gyro energy high.
            if leg_reader is not None and leg_reader.connected:
                if leg.state == "squat":
                    self._aim_frozen = True
                elif leg.tilt <= LegTracker.SQUAT_TILT_OFF:
                    self._aim_frozen = False
                if self._aim_frozen:
                    self.rel = 0
                    return
            roll = self._axis(self.state.roll_axis, self.state.roll_sign)
            # delta_time is ms; convert to seconds for a sensible rad/sec rate.
            self.angle += roll * self.state.turn_rate * (self.game.delta_time / 1000.0)
            self.rel = int(roll * 10)

        def get_damage(self, damage):
            # Monster damage halved (min 1 so hits still register).
            super().get_damage(max(1, int(damage // 2)))

    return RingPlayer


# ----- Weapon loadout: three variants riffing on the base shotgun -----

WEAPON_SPECS = [
    # name, damage, animation_time (smaller = faster fire), scale, tint RGBA
    # Damage is doubled from the base tuning (player buff).
    {"name": "Shotgun", "damage": 100, "anim": 90,  "scale": 0.40, "tint": None},
    {"name": "Pistol",  "damage": 36,  "anim": 30,  "scale": 0.30, "tint": (255, 240, 140, 255)},
    {"name": "BFG",     "damage": 220, "anim": 180, "scale": 0.46, "tint": (140, 255, 180, 255)},
]


def _tint(surface, color):
    """Multiplicative tint that preserves alpha so the sprite cutout stays clean."""
    out = surface.copy()
    overlay = pg.Surface(out.get_size(), pg.SRCALPHA)
    overlay.fill(color)
    out.blit(overlay, (0, 0), special_flags=pg.BLEND_RGBA_MULT)
    return out


class WeaponLoadout:
    """Quacks like base Weapon (update / draw / reloading / damage) but holds
    a list of weapon variants. The Ring-Con's "pull" cycles between them."""

    def __init__(self, game):
        self.game = game
        self.weapons = []
        for spec in WEAPON_SPECS:
            w = Weapon(game, scale=spec["scale"], animation_time=spec["anim"])
            w.damage = spec["damage"]
            w.name = spec["name"]
            if spec["tint"] is not None:
                tinted = [_tint(img, spec["tint"]) for img in w.images]
                w.images = deque(tinted)
                w.image = w.images[0]
                # AnimatedSprite re-positions weapon_pos off image size; redo it.
                w.weapon_pos = (st.HALF_WIDTH - w.images[0].get_width() // 2,
                                st.HEIGHT - w.images[0].get_height())
            self.weapons.append(w)
        self.idx = 0
        self.label_font = pg.font.SysFont("Helvetica", 22, bold=True)

    @property
    def active(self):
        return self.weapons[self.idx]

    # Compatibility with the base Weapon interface used elsewhere.
    @property
    def reloading(self):
        return self.active.reloading

    @reloading.setter
    def reloading(self, v):
        self.active.reloading = v

    @property
    def damage(self):
        return self.active.damage

    def update(self):
        self.active.update()

    def draw(self):
        self.active.draw()
        # HUD: weapon name in top-left.
        label = self.label_font.render(self.active.name, True, (255, 230, 160))
        shadow = self.label_font.render(self.active.name, True, (0, 0, 0))
        self.game.screen.blit(shadow, (12, 11))
        self.game.screen.blit(label, (10, 10))

    def cycle(self):
        # Don't switch mid-reload; the animation belongs to one weapon.
        if not self.active.reloading:
            self.idx = (self.idx + 1) % len(self.weapons)
            print(f"[weapon] now: {self.active.name}", flush=True)


# ----- Game subclass that wires fire/cycle into the loop -----

class RingGame(doom_main.Game):
    def __init__(self, state, reader, leg_reader, Player_cls):
        self.state = state
        self.reader = reader
        self.leg_reader = leg_reader
        # Patch globals new_game() pulls from so subsequent new_game() calls
        # (on respawn) also use our subclasses.
        doom_main.Player = Player_cls
        doom_main.Weapon = WeaponLoadout
        super().__init__()
        # Base Game grabs the mouse and hides the cursor; we don't need that.
        pg.event.set_grab(False)
        pg.mouse.set_visible(True)

    def _shutdown(self):
        # Stop the reader threads and let them close their native HID handles
        # BEFORE the interpreter tears down. A daemon thread blocked in
        # cython-hidapi's read() during shutdown segfaults the process on quit
        # (exit 139). Mirrors the clean stop the other games already do.
        for r in (self.reader, self.leg_reader):
            if r is not None:
                r.stop()
        for r in (self.reader, self.leg_reader):
            if r is not None:
                r.join(timeout=0.5)

    def _handle_config_key(self, event):
        s = self.state
        shift = bool(event.mod & pg.KMOD_SHIFT)
        if event.key == pg.K_p:
            if shift:
                s.pitch_sign *= -1
            else:
                s.pitch_axis = (s.pitch_axis + 1) % 3
            print(f"[cfg] pitch axis={s.pitch_axis} sign={s.pitch_sign}", flush=True)
        elif event.key == pg.K_r:
            if shift:
                s.roll_sign *= -1
            else:
                s.roll_axis = (s.roll_axis + 1) % 3
            print(f"[cfg] roll axis={s.roll_axis} sign={s.roll_sign}", flush=True)
        elif event.key == pg.K_COMMA:
            s.turn_rate = max(0.3, s.turn_rate - 0.3)
            print(f"[cfg] turn_rate={s.turn_rate:.2f}", flush=True)
        elif event.key == pg.K_PERIOD:
            s.turn_rate = min(8.0, s.turn_rate + 0.3)
            print(f"[cfg] turn_rate={s.turn_rate:.2f}", flush=True)

    def check_events(self):
        # Drain events ourselves so config keys land before the base game
        # consumes the queue.
        self.global_trigger = False
        for event in pg.event.get():
            if event.type == pg.QUIT or (event.type == pg.KEYDOWN and event.key == pg.K_ESCAPE):
                self._shutdown()
                pg.quit()
                sys.exit()
            elif event.type == self.global_event:
                self.global_trigger = True
            elif event.type == pg.KEYDOWN:
                self._handle_config_key(event)
            self.player.single_fire_event(event)
        # Continuous fire: while push held and the shotgun isn't mid-reload,
        # trigger a shot. The weapon's reload animation gates the rate.
        if self.state.fire_held and not self.weapon.reloading and not self.player.shot:
            self.sound.shotgun.play()
            self.player.shot = True
            self.weapon.reloading = True
        # Cycle pulse: switch active weapon.
        if self.state.cycle_pulse:
            self.state.cycle_pulse = False
            self.weapon.cycle()

    def update(self):
        super().update()
        # Overlay a tiny axis/tilt readout so we can debug axis swaps.
        s = self.state
        a, r = s.accel, s.rest_accel
        if a is not None and r is not None:
            pitch_raw = a[s.pitch_axis] - r[s.pitch_axis]
            roll_raw = a[s.roll_axis] - r[s.roll_axis]
            txt = (f"pitch[axis={s.pitch_axis} sgn={s.pitch_sign:+d}] {pitch_raw:+5.0f}   "
                   f"roll[axis={s.roll_axis} sgn={s.roll_sign:+d}] {roll_raw:+5.0f}   "
                   f"turn={s.turn_rate:.1f}  strain={s.strain}  fire={s.fire_held}  "
                   f"(keys: p/P r/R , .)")
            font = pg.font.SysFont("Helvetica", 14)
            surf = font.render(txt, True, (255, 255, 255))
            self.screen.blit(surf, (8, st.HEIGHT - 22))
            pg.display.flip()


# ----- Calibration -----

def calibrate(state, reader, screen):
    big = pg.font.SysFont("Helvetica", 36, bold=True)
    mid = pg.font.SysFont("Helvetica", 22)
    sml = pg.font.SysFont("Helvetica", 16)
    clock = pg.time.Clock()
    accel_samples, strain_samples = [], []
    target = 90
    while len(accel_samples) < target:
        clock.tick(60)
        for evt in pg.event.get():
            if evt.type == pg.QUIT or (evt.type == pg.KEYDOWN and evt.key == pg.K_ESCAPE):
                pg.quit()
                sys.exit(0)
        if reader.status == "connected" and state.accel is not None and state.strain is not None:
            accel_samples.append(state.accel)
            strain_samples.append(state.strain)
            msg = f"Calibrating rest pose… {int(100 * len(accel_samples) / target)}%"
            msg_color = (220, 220, 220)
        else:
            msg = f"Joy-Con: {reader.status}"
            msg_color = (220, 100, 100)
        screen.fill((10, 10, 22))
        t = big.render("Hold ring steady", True, (250, 230, 180))
        screen.blit(t, (st.HALF_WIDTH - t.get_width() // 2, st.HALF_HEIGHT - 90))
        t = mid.render(msg, True, msg_color)
        screen.blit(t, (st.HALF_WIDTH - t.get_width() // 2, st.HALF_HEIGHT - 20))
        for i, line in enumerate([
            "Stand neutral. Don't squeeze, don't tilt.",
            "Hold the ring so 'forward tilt' = lean toward the screen.",
        ]):
            t = sml.render(line, True, (150, 150, 160))
            screen.blit(t, (st.HALF_WIDTH - t.get_width() // 2,
                            st.HALF_HEIGHT + 20 + 22 * i))
        pg.display.flip()
    state.rest_accel = tuple(sum(s[i] for s in accel_samples) / len(accel_samples)
                             for i in range(3))
    state.rest_strain = sum(strain_samples) / len(strain_samples)


def main():
    pg.init()
    screen = pg.display.set_mode(st.RES)
    pg.display.set_caption("Ring DOOM")

    state = RingState()
    reader = RingReader(state)
    reader.start()

    # Leg Joy-Con (optional): run-to-move + squat-to-freeze-aim. Starts now so
    # it self-calibrates during the ring calibration screen below.
    leg = LegTracker()
    leg_info = find_joycon(side="L")
    leg_reader = LegReader(leg_info, leg) if leg_info else None
    if leg_reader:
        leg_reader.start()

    calibrate(state, reader, screen)

    PlayerCls = make_ring_player_cls(state, leg, leg_reader)
    game = RingGame(state, reader, leg_reader, PlayerCls)
    game.run()


if __name__ == "__main__":
    main()
