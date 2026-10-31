"""One place where a second load is kept from starting while the first one is starting.

The house battery's grid charging and the car's wallbox share the main breaker. Each of their
writers takes the gate for the stretch in which it starts something and gives it back after;
a writer that finds it taken does not start and says why. Everything runs on the event loop,
so the test-and-set below cannot be interrupted. The gate only ever holds starting back: stopping
and protective steps never ask for it.
"""

from __future__ import annotations


class StartGate:
    def __init__(self) -> None:
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def try_acquire(self) -> bool:
        if self._held:
            return False
        self._held = True
        return True

    def release(self) -> None:
        self._held = False
