"""Shared helpers for the deploy scripts: env files and secrets.

Both `render_deploy.py` and `hf_deploy.py` need the same three things — read `.env`,
require a set of keys, and pull a token from an environment variable or a secret file.
Keeping that here means the lookup order and the error messages exist once.
"""

from __future__ import annotations

import pathlib
import sys

from dotenv import dotenv_values

PLACEHOLDER = 'your_'


def read_env(path: str = '.env') -> dict[str, str]:
    """Parse a .env file, dropping unset entries."""
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def require(env: dict[str, str], keys: tuple[str, ...], source: str = '.env') -> None:
    """Exit with one message listing every missing key."""
    missing = [key for key in keys if not env.get(key)]
    if missing:
        sys.exit(f'{source} is missing: {", ".join(missing)}')


def secret_from_files(
    key: str, candidates: list[pathlib.Path], *, allow_bare: bool = False
) -> str | None:
    """Return KEY's value from the first candidate file that provides it.

    Looks for a `KEY=value` line; with allow_bare, a file holding only the value
    (like ~/.render-api-key) also works.
    """
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_text(encoding='utf-8')
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(f'{key}='):
                value = stripped.split('=', 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
        if allow_bare and raw.strip():
            return raw.strip()
    return None


def client_secret(
    key: str,
    candidates: list[pathlib.Path],
    hint: str,
    *,
    allow_bare: bool = False,
    env_var: str | None = None,
) -> str:
    """Environment first, then files. Exits with `hint` if nothing usable is found."""
    import os

    value = os.getenv(env_var or key)
    if not value:
        value = secret_from_files(key, candidates, allow_bare=allow_bare)
    if not value:
        sys.exit(hint)
    if PLACEHOLDER in value:
        sys.exit(f'{key} still contains the placeholder. {hint}')
    return value.strip()
