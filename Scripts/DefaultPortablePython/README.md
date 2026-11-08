# Portable projects

Run `./portable` to try the included example. Nothing is downloaded by the
launcher or the default command files. The supplied Python 3.13.13 archive has
been extracted into `tools/python`; the original archive is retained in `tools/`.

```text
portablepython/                 # Rename or move this entire folder freely.
├── portable                    # Bash launcher
├── commands/
│   └── default/
│       ├── build               # Editable Bash build instructions
│       └── run                 # Editable Bash run instructions
├── projects/
│   └── default/
│       └── launch.py           # Replace with your project
├── tools/
│   ├── python/                 # Supplied Python, including pip
│   └── ...                     # Add your own Node.js or other runtimes
├── portable-home/              # Shared home, configuration, caches, temp files
└── support/
    └── relocate_python.py      # Keeps Python console commands movable
```

## Commands

```bash
./portable                              # Same as ./portable run --program default
./portable build                        # commands/default/build
./portable run --serve 0.0.0.0            # Pass arguments to commands/default/run
./portable build --program myapp
./portable run --program myapp --port 8080
./portable shell                        # Interactive Bash in projects/default
./portable shell --program myapp
./portable help
```

Every command uses the `default` program unless you specify `--program NAME`
or `--program=NAME`. Build and run forward remaining arguments unchanged to
the command file. Put `--` before application arguments if your application
itself needs an option called `--program`:

```bash
./portable run --program myapp -- --program application-option
```

## Add your project

Put your application files in `projects/default` and edit `commands/default/build`
and `commands/default/run`. These are Bash scripts, with their working directory
set to the matching project. They need not be executable. For example:

```bash
# commands/default/build
set -e
python -m pip install -r requirements.txt "$@"
```

```bash
# commands/default/run
set -e
exec python launch.py "$@"
```

`"$@"` forwards arguments; keep the quotes. `set -e` stops on a failed step.
`exec` on the final run command lets the program receive signals directly and
preserves its exit status. You can write multiple steps, use absolute paths, or
refer to `$PORTABLE_ROOT`, `$PORTABLE_PROJECT`, and `$PORTABLE_PROGRAM`.

The example pip install downloads dependencies only when you put it into your
build file and explicitly run the build. To build offline, supply wheels yourself:

```bash
python -m pip install --no-index --find-links wheels -r requirements.txt
```

For additional projects, create matching folders, for example
`commands/myapp/{build,run}` and `projects/myapp/`. Copy the default command files
as a starting point. Folder names can contain spaces when quoted on the CLI.
All projects share the runtimes, their installed packages, and `portable-home`.
Use separate portable folders when projects need conflicting Python dependencies.

## Add runtimes

Extract a runtime into `tools/NAME`, such as `tools/node`. The launcher adds
each runtime's `bin` directory and root to PATH, followed by other directories
under `tools` containing executable files and any nested `bin`, `sbin`, or
`Scripts` directories. Tool directories are ordered alphabetically; earlier
PATH entries win when names collide. Executables must have execute permission.
Keep dependencies and symlink targets inside the portable folder.

For Node.js, the run file could simply contain:

```bash
exec node server.js "$@"
```

The host's absolute PATH entries remain available after portable tools, so
installed host commands such as Git remain callable. Nothing copies Git or its
system libraries into this folder. The destination PC must also have Git if
your project needs it. Git uses `portable-home/.gitconfig` for user configuration;
your host user's Git identity and credentials are not imported automatically.

## Home and configuration

The launcher sets HOME, all standard XDG directories, temporary directories,
Bash history, pip cache, npm cache/global prefix, Node history, and Cargo/Rustup
homes to locations under `portable-home`. It clears inherited Python/virtual
environment settings and pip installation destinations, disables Python's user
site, and disables pip configuration files so packages go into the bundled
Python. Bash startup files from the host are not loaded.

These settings affect only the launched process and its children. Applications
that respect these settings keep their files here. This is environment isolation,
not a filesystem sandbox: a program with hardcoded paths, system-account lookups,
or other inherited application-specific settings can still write elsewhere.
For programs with their own cache or model-directory variables, set those in
your command file or in an optional `portable.conf` at this folder's root:

```bash
# portable.conf is trusted Bash, sourced after the default environment is set.
export HF_HOME="$HOME/.cache/huggingface"
# export MY_APP_DATA="$PORTABLE_PROJECT/data"
```

To deliberately use a different home, pass an absolute path:

```bash
PORTABLE_HOME=/some/other/home ./portable shell
```

## Moving to another PC

Move the **whole folder**, preserving permissions and symlinks. Paths are
calculated from the launcher's current location; paths containing spaces work.
The Python helper automatically changes absolute Python interpreter headers in
`tools/python/bin` into relative launchers, including pip's long-path shell
headers. This keeps ordinary pip console commands usable after a move, without
reinstalling packages. The helper makes atomic changes only when needed.

Avoid ordinary Python virtual environments for this setup: they embed absolute
paths and are not designed to be moved. Install into the bundled Python instead.
Application configuration, editable installs, native build artifacts, other
runtimes, and external symlinks can also contain absolute paths; choose portable
software and use paths relative to the project or `$PORTABLE_ROOT`.

This launcher targets Linux with Bash 4.4+ and standard GNU utilities (`find`,
`sort`, `readlink`, etc.). The supplied runtime targets **x86-64 Linux with glibc**;
the destination must support it and any native dependencies used by your apps.
Moving this runtime to Windows, macOS, ARM, or an incompatible Linux system
requires supplying suitable runtimes and, where needed, rebuilding dependencies.

## Checks

Run the offline integration checks with:

```bash
tools/python/bin/python3 tests/test_portable.py
```

They exercise argument forwarding, multiple projects, portable directories,
host Git access, errors, shell startup isolation, and a local wheel's console
command after moving the entire test folder. No network is used.
