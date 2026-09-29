"""Set the saved size-ramp step back to launch_fraction.

    python -m scripts.reset_ramp [--reason "..."]

For use while the bot is stopped: the next start reads step 0 and stays
there. While the bot is running use Telegram /resetramp instead, since the
running process holds the step in memory and would overwrite this.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from predcup.store import EventStore


def reset_saved_ramp_step(db_path: str | Path, reason: str) -> None:
    store = EventStore(db_path)
    previous = store.load_ramp_step()
    store.save_ramp_step(0)
    store.log(
        "size_ramp",
        {"action": "reset", "step": 0, "previous_step": previous, "detail": reason, "source": "cli"},
    )
    store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--reason", default="manual reset via scripts/reset_ramp.py")
    args = parser.parse_args()
    with open(args.config) as f:
        db_path = yaml.safe_load(f)["storage"]["db_path"]
    reset_saved_ramp_step(db_path, args.reason)
    print(f"size ramp reset to step 0 in {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
