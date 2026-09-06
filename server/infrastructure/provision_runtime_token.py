#!/usr/bin/env python3
"""
Atomic Runtime Token Provisioner for Taktstock Sidecars (Codex & AGY)
--------------------------------------------------------------------
Extracts the specified token from the single master source and publishes it atomically to tmpfs.
Security:
- Canonicalizes exclusively the parent directory, never the destination file target.
- Reconstructs the destination with lexical basename and explicitly rejects symlinks (lstat).
- Opens with O_CREAT | O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, sets 0400 permissions, fsync and os.replace.
- Never prints secrets to stdout/stderr.
"""

import os
import sys
import stat
import uuid
import argparse
from pathlib import Path

MIN_TOKEN_LENGTH = 32


def provision_token(env_file: Path, dest_file: Path, token_var: str = "TAKTSTOCK_SIDECAR_TOKEN") -> bool:
    token = ""
    var_candidates = [token_var]
    if token_var == "TAKTSTOCK_SIDECAR_TOKEN":
        var_candidates.append("UFFICIO_SIDECAR_TOKEN")
    elif token_var == "UFFICIO_SIDECAR_TOKEN":
        var_candidates.insert(0, "TAKTSTOCK_SIDECAR_TOKEN")

    for var in var_candidates:
        token = os.environ.get(var, "").strip()
        if token:
            break

    if not token and env_file and env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            for var in var_candidates:
                prefix = f"{var}="
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith(prefix):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                if token:
                    break
        except Exception:
            pass

    if len(token) < MIN_TOKEN_LENGTH:
        sys.stderr.write(f"ERROR: {token_var} missing or shorter than 32 characters.\n")
        return False

    dest_path = Path(dest_file)
    # Canonicalize EXCLUSIVELY the parent directory (not the destination file)
    dest_dir = dest_path.parent.resolve()
    if not dest_dir.exists() or not dest_dir.is_dir():
        sys.stderr.write(f"ERROR: Destination directory {dest_dir} not found or invalid.\n")
        return False

    # Lexical reconstruction of the destination path (no symlink following)
    dest = dest_dir / dest_path.name

    # Categorical rejection if the pre-existing destination is a symlink (lstat check)
    try:
        st = os.lstat(str(dest))
        if stat.S_ISLNK(st.st_mode):
            sys.stderr.write("ERROR: Token destination is a prohibited symlink.\n")
            return False
    except FileNotFoundError:
        pass
    except Exception as e:
        sys.stderr.write(f"ERROR: Cannot verify destination status: {e}\n")
        return False

    # Unique temporary file on the same filesystem (tmpfs) to enable atomic rename(2)
    tmp_file = dest_dir / f".token.tmp.{os.getpid()}_{uuid.uuid4().hex[:8]}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

    try:
        fd = os.open(str(tmp_file), flags, 0o400)
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_file), str(dest))
        return True
    except Exception:
        sys.stderr.write("ERROR: Failure during atomic write of runtime token.\n")
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass
        return False


def main():
    parser = argparse.ArgumentParser(description="Provision runtime token atomically.")
    parser.add_argument("--env-file", type=Path, help="Path to sidecar.env master file")
    parser.add_argument("--dest", type=Path, required=True, help="Destination token file path")
    parser.add_argument(
        "--token-var", "--var-name",
        dest="token_var",
        type=str,
        default="TAKTSTOCK_SIDECAR_TOKEN",
        help="Environment variable name for the token (default: TAKTSTOCK_SIDECAR_TOKEN)"
    )
    args = parser.parse_args()

    success = provision_token(args.env_file, args.dest, token_var=args.token_var)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
