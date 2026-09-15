"""Track the backend actually initialized by the current worker.

Two different things travel under this name:

* how the MOTION FIELD is estimated - the worker's own optical-flow path
  (NVOFA) or a CPU fallback;
* which NEURAL PASS runs - the NVIDIA NGX feature (default) or the AMD
  runtime on a Radeon (NS_MOTION_BACKEND=amd).

They share one switch because a machine has one answer to both: a Radeon
cannot run NVOFA either, and "AMD" on the menu means "this machine is a
Radeon, run its own neural pass". The worker reads the same variable and
picks accordingly.
"""

BACKENDS = ("cpu", "nvofa", "amd")


def normalize_backend(value):
    """The stored value, or the nearest usable one.

    An unknown string (an old config, a hand edit) falls back to cpu -
    never to amd, which would quietly change which card runs the pass.
    """
    return value if value in BACKENDS else "cpu"


class MotionBackendStatus:
    def __init__(self):
        self.worker = None
        self.active = False
        self.failed = False
        self.amd = False
        self._scanned = 0

    def update(self, worker, logs):
        if worker is not self.worker:
            self.worker = worker
            self.active = self.failed = self.amd = False
        # Keep the last verdict when the bounded log buffer rotates.
        for line in reversed(logs):
            if "[amd] the runtime did not come up:" in line:
                self.active, self.failed, self.amd = False, True, False
                break
            if "[amd] ===== AMD path active =====" in line:
                self.active, self.failed, self.amd = True, False, True
                break
            if "[nvofa] unavailable:" in line:
                self.active, self.failed, self.amd = False, True, False
                break
            if "[nvofa] active:" in line:
                self.active, self.failed, self.amd = True, False, False
                break
        return self.active
