"""Create the encryption key for the ServiceNow / Jira secrets: `python -m itsm_adapter.keygen [--env PATH]`.

Adds ITSM_SECRET_KEY to the .env file (the nearest one above the current directory unless --env is given) and never
prints the key. An existing key is left alone: changing it makes the stored secrets unreadable (they would have to
be entered again in the UI).
"""

import argparse
import sys
from pathlib import Path

from cryptography.fernet import Fernet

NAME = "ITSM_SECRET_KEY"


def find_env(start: Path) -> Path:
    for d in [start, *start.parents]:
        if (d / ".env").is_file() or (d / ".env.example").is_file():
            return d / ".env"
    return start / ".env"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", type=Path, help="the .env file to write (default: nearest .env)")
    path = parser.parse_args().env or find_env(Path.cwd())
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in text.splitlines():
        if line.strip().startswith(f"{NAME}=") and line.split("=", 1)[1].strip():
            print(f"{NAME} is already set in {path}; left unchanged.")
            sys.exit(0)
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(f"{NAME}=")]
    lines.append(f"{NAME}={Fernet.generate_key().decode()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{NAME} written to {path} (not shown). Restart the itsm-adapter containers to use it.")


if __name__ == "__main__":
    main()
