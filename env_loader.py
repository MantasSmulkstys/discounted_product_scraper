from __future__ import annotations

from pathlib import Path


def load_env(path: str | Path | None = None, override: bool = False) -> None:
    env_path = Path(path) if path is not None else Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    import os

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if not override and key in os.environ:
            continue
        os.environ[key] = value
