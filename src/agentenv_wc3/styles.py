"""The env's entry in its image: the game style the image was built for (WC3_STYLE). `raw` (env wc3) takes each
unit's orders by id through general tools; `commander` (env wc3-commander) plays through wc3agent's interface
(commander.py)."""

from __future__ import annotations

import logging
import os
import sys

from .commander import CommanderEnv
from .server import WC3Env

STYLES = {"raw": WC3Env, "commander": CommanderEnv}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    style = os.environ.get("WC3_STYLE") or "raw"
    if style not in STYLES:
        raise SystemExit(f"WC3_STYLE must be one of {', '.join(STYLES)}, not {style!r}")
    STYLES[style]().serve()


if __name__ == "__main__":
    main()
