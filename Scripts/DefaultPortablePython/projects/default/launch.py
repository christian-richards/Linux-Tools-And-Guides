"""Replace this demonstration with your project, then edit its command files."""

import os
from pathlib import Path
import sys

print("Portable project is ready.")
print(f"Python: {sys.executable}")
print(f"Project: {Path.cwd()}")
print(f"Home: {os.environ['HOME']}")
print(f"Arguments: {sys.argv[1:]}")
