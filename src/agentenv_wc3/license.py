"""Your Warcraft III activation files (roc.w3k, tft.w3k) on the harness's side. The tasks' add_license steps
give them to the env from agent-env's secret store, where each is a secret holding the file's base64 (LICENSE_SECRETS);
`agent-env wc3 license import` puts them there from a folder (license_dir)."""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from agent_env.config import get_config

PLUGIN = "agentenv-wc3"
LICENSE_FILES = ("roc.w3k", "tft.w3k")
LICENSE_SECRETS = {"roc.w3k": "WC3_ROC_W3K", "tft.w3k": "WC3_TFT_W3K"}
DEFAULT_LICENSE_DIR = "~/.wc3-license"


def license_dir(given: str | None = None) -> Path:
    """Where your activation files are: the step's `license_dir`, else `license_dir` in [plugins.agentenv-wc3] of
    .agentenv/config.toml, else $WC3_LICENSE_DIR, else ~/.wc3-license."""
    if given is None:
        try:
            from agent_env.plugins import settings

            given = settings(PLUGIN).get("license_dir")
        except Exception:   # no config file: the other places still apply
            given = None
    return Path(given or os.environ.get("WC3_LICENSE_DIR") or DEFAULT_LICENSE_DIR).expanduser()


def license_from_secrets(names: dict[str, str]) -> dict[str, str] | None:
    """The activation files from agent-env's secret store ([stores.secret]; each secret is a file in base64), so a
    run on any machine can start the real game: None when neither secret is there."""
    store = get_config().get_secret_store()
    found = {f: store.get(key) for f, key in names.items()}
    if not any(found.values()):
        return None
    if missing := [names[f] for f, value in found.items() if not value]:
        raise RuntimeError(f"the secret store has only some activation files: {', '.join(missing)} "
                           "missing (agent-env wc3 license import stores both)")
    for f, value in found.items():
        try:
            if not base64.b64decode(value, validate=True):
                raise binascii.Error("empty")
        except binascii.Error as e:
            raise RuntimeError(f"the secret {names[f]} is not {f} in base64 ({e})") from e
    return found


def read_license(directory: Path) -> dict[str, str] | None:
    """The activation files in `directory`, as the secret store keeps them: {name: base64}; None when either is
    missing or empty."""
    if any(not (directory / n).is_file() or not (directory / n).stat().st_size for n in LICENSE_FILES):
        return None
    return {n: base64.b64encode((directory / n).read_bytes()).decode() for n in LICENSE_FILES}
