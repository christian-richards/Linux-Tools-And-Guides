"""Keep pip-installed Python commands movable using only the standard library.

Pip uses either an absolute shebang or a three-line shell trampoline (for long
paths or paths with spaces). Replace just that header, preserving the Python
body. No environment, package metadata, or third-party code is patched.
"""

import os
from pathlib import Path
import re
import shlex
import sys
import tempfile


def relocate(bin_dir):
    for script in bin_dir.iterdir():
        # Leave interpreter binaries, symlinks, and non-executable data alone.
        if script.is_symlink() or not script.is_file():
            continue
        if not os.access(script, os.X_OK):
            continue
        with script.open("rb") as stream:
            if stream.read(2) != b"#!":
                continue
            stream.seek(0)
            lines = [stream.readline() for _ in range(3)]
        try:
            if lines[0].startswith(b"#!/") and b"python" in lines[0]:
                command = shlex.split(os.fsdecode(lines[0][2:].strip()))
                header_lines = 1
            elif (lines[0] == b"#!/bin/sh\n"
                  and lines[1].startswith(b"'''exec' ")
                  and lines[2] == b"' '''\n"):
                command = shlex.split(os.fsdecode(lines[1][len(b"'''exec' "):]))
                # Distlib appends "$0" "$@" to the interpreter command.
                if command[-2:] != ["$0", "$@"]:
                    continue
                command = command[:-2]
                header_lines = 3
            else:
                continue
        except ValueError:
            continue
        if not command or not command[0].startswith("/"):
            continue
        interpreter = Path(command[0]).name
        if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", interpreter):
            continue
        if not (bin_dir / interpreter).is_file():
            continue

        # A shell/Python polyglot header like the one shipped by Astral. Resolve
        # symlinks too, so invoking a command through a symlink still works.
        flags = "".join(" " + shlex.quote(flag) for flag in command[1:])
        header = (
            '#!/bin/sh\n'
            f'\'\'\'exec\' "$(dirname -- "$(readlink -f -- "$0")")/{interpreter}"'
            f'{flags} "$0" "$@"\n'
            "' '''\n"
        ).encode()
        body = script.read_bytes()[sum(map(len, lines[:header_lines])):]
        # Replace atomically: concurrent launches never see half-written files.
        with tempfile.NamedTemporaryFile(dir=bin_dir, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(header + body)
                stream.flush()
                os.chmod(temporary, script.stat().st_mode)
                os.replace(temporary, script)
            finally:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    relocate(Path(sys.argv[1]))
