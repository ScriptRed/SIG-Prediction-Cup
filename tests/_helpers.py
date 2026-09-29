"""Shared test helpers. Importable as `_helpers` because tests/ has no
__init__.py, so pytest's default import mode puts this directory on
sys.path."""

from predcup.risk import SizeRamp, SizeRampConfig
from predcup.store import EventStore


class _NullAlerter:
    def send(self, message: str) -> None:
        pass


def full_size_ramp() -> SizeRamp:
    """A ramp already at full size, for tests of limits other than the
    ramp. RiskManager refuses to construct without one (fail closed)."""
    config = SizeRampConfig(
        launch_fraction=1.0,
        step_multiplier=2.0,
        clean_reconciliations_per_step=1,
        rate_limit_max_in_window=5,
        rate_limit_window_seconds=600,
    )
    return SizeRamp(config, event_store=EventStore(":memory:"), alerter=_NullAlerter())
