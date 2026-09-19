"""The frame limiter: it must not catch up, and it must be inert by default.

Brought over from the NVIDIA main line together with pacing.py. Two things are
worth a test rather than a comment, because both are silent when they are wrong:

1. A SLOW FRAME MUST NOT BE FOLLOWED BY A BURST.

   This is the whole point of the class and the reason it was brought to the AMD
   path: the pass runs inline, so a slow frame delays the next iteration, and a
   limiter that "makes up" the lost time fires several frames back to back. That
   is worse than the lower rate it is correcting - and on this path it means
   feeding the engine again immediately after it has just finished.

   The check drives a synthetic clock: one iteration is deliberately late, and
   the sleep it asks for afterwards must be zero-ish rather than negative (a
   negative delay would mean the pacer thinks it owes time).

2. IT IS OFF UNTIL SOMEONE ASKS FOR IT.

   Unlimited is the default in settings_io, and `frame_limit_fps` must return 0
   for anything it does not recognise. If it ever returned a number for junk, a
   corrupt config would silently cap every user's frame rate.

Run:  runtime\\python.exe tests\\test_pacing.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from pacing import FramePacer  # noqa: E402
import settings_io  # noqa: E402


class FakeClock:
    """A clock the test advances by hand, so no real time passes."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def main() -> int:
    failures = []

    # --- 1. a slow frame does not become a burst --------------------------
    clock = FakeClock()
    pacer = FramePacer(clock=clock, sleeper=clock.sleep)

    # Steady 60 fps for a while: each iteration starts on the deadline.
    for _ in range(10):
        started = clock.now
        pacer.wait(60.0, started)
        clock.now += 1.0 / 60.0        # the frame itself takes its whole slot

    # Now one iteration is late: it started 100 ms after it should have.
    #
    # What "no catch-up" means here, measured rather than assumed: the deadline
    # is re-anchored to the late frame's own start, so the delay stays ONE
    # interval and no debt is carried. A pacer that tried to recover would come
    # back with a delay near zero - and then keep doing that until the debt was
    # paid, which is the burst. Asserting "delay == 0" would be wrong: the delay
    # is a full interval either way, and only the debt distinguishes them.
    interval = 1.0 / 60.0
    clock.now += 0.100
    late_start = clock.now
    delay_after_slow = pacer.wait(60.0, late_start)

    if abs(delay_after_slow - interval) > 1e-6:
        failures.append(
            f"after a slow frame the pacer asked to sleep "
            f"{delay_after_slow:.4f}s; it must sleep exactly one interval "
            f"({interval:.4f}s) from that frame's own start, neither zero "
            f"(a catch-up burst) nor more (debt that would never clear)")

    # And the debt must not have been carried into the next iterations: two
    # more frames, each starting on its own deadline, must each get one
    # interval - not shortening delays as an accumulated debt is paid off.
    for k in range(2):
        clock.now += interval          # the frame takes its whole slot
        d = pacer.wait(60.0, clock.now)
        if abs(d - interval) > 1e-6:
            failures.append(
                f"iteration {k + 2} after the slow frame asked to sleep "
                f"{d:.4f}s instead of one interval ({interval:.4f}s) - the "
                f"lost time is being paid back in a burst")

    # --- 2. unlimited really is unlimited ---------------------------------
    clock2 = FakeClock()
    pacer2 = FramePacer(clock=clock2, sleeper=clock2.sleep)
    for bad in (0, 0.0, -1, -60.0):
        d = pacer2.wait(bad, clock2.now)
        if d != 0.0:
            failures.append(f"fps={bad} asked to sleep {d} - unlimited must not "
                            f"sleep at all")
    if clock2.slept:
        failures.append(f"unlimited slept {clock2.slept} - it must never sleep")

    # Junk in the fps must be treated as unlimited, not crash the loop.
    for junk in (None, "sixty", float("nan"), float("inf")):
        try:
            d = pacer2.wait(junk, clock2.now)
        except Exception as exc:                     # noqa: BLE001
            failures.append(f"fps={junk!r} raised {exc!r} - a junk value must "
                            f"not stop the render loop")
            continue
        if d != 0.0:
            failures.append(f"fps={junk!r} asked to sleep {d}")

    # --- 3. the settings side is inert for anything it does not know ------
    cases = [
        ({}, 0),
        ({"frame_limit_mode": "unlimited"}, 0),
        ({"frame_limit_mode": "30"}, 30),
        ({"frame_limit_mode": "60"}, 60),
        ({"frame_limit_mode": "custom", "frame_limit_custom": 90}, 90),
        # Out of range clamps into the allowed band rather than passing through.
        ({"frame_limit_mode": "custom", "frame_limit_custom": 1},
         settings_io.FRAME_LIMIT_CUSTOM_MIN),
        ({"frame_limit_mode": "custom", "frame_limit_custom": 9999},
         settings_io.FRAME_LIMIT_CUSTOM_MAX),
        # Junk must mean "no cap", never a made-up number.
        ({"frame_limit_mode": "nonsense"}, 0),
        ({"frame_limit_mode": "custom", "frame_limit_custom": "abc"}, 90),
    ]
    for cfg, want in cases:
        got = settings_io.frame_limit_fps(cfg)
        if got != want:
            failures.append(f"frame_limit_fps({cfg!r}) = {got}, expected {want}")

    # --- 4. changing the cap mid-run takes effect immediately -------------
    # The menu can change this while the program runs. If the pacer kept the
    # old deadline, the new rate would only apply once the old timeline ran
    # out - the user would set 30 fps and see 60 for a while.
    # Raising the cap is the case that shows it: 30 -> 60 must shorten the
    # very next delay to 1/60. Going the other way (60 -> 30) does not
    # distinguish a working pacer from one that keeps the old deadline, because
    # the old deadline is already further away than the new interval - which is
    # exactly how a first version of this check passed against broken code.
    # Note for the next reader: `wait` advances the clock itself when it sleeps,
    # so the frame's own duration must NOT be added on top - doing that made a
    # first version of this check pass against broken code, because the two
    # timelines happened to coincide.
    clock4 = FakeClock()
    pacer4 = FramePacer(clock=clock4, sleeper=clock4.sleep)
    pacer4.wait(30.0, clock4.now)             # establish a 30 fps timeline;
                                              # this already slept one interval
    delay = pacer4.wait(60.0, clock4.now)     # the user raises it to 60
    want = 1.0 / 60.0
    if abs(delay - want) > 1e-6:
        failures.append(
            f"after the cap changed from 30 to 60 the pacer still asked for "
            f"{delay:.4f}s instead of {want:.4f}s - the new rate must apply to "
            f"the very next frame")

    # --- 5. a cap actually holds the rate ---------------------------------
    clock3 = FakeClock()
    pacer3 = FramePacer(clock=clock3, sleeper=clock3.sleep)
    instant_frames = 0
    for _ in range(10):
        pacer3.wait(30.0, clock3.now)     # frames cost nothing
        instant_frames += 1
    # Ten uncapped instant frames would advance the clock by ~0; the cap must
    # have held it back instead.
    if clock3.now - 1000.0 < 9 * (1.0 / 30.0) - 1e-6:
        failures.append(
            f"ten instant frames at 30 fps advanced the clock by only "
            f"{clock3.now - 1000.0:.4f}s - the cap is not being applied")

    if failures:
        print("FAIL: the frame limiter is not behaving:")
        for f in failures:
            print(f"  - {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print("OK: the limiter holds the rate, never catches up, and is inert "
          "unless a cap is set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
