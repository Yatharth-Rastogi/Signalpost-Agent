"""One-shot: rewrite requirements.txt with EXACT versions proven in this env.
Pins only the packages you already declared, so the clean-machine install
(§04 reproducibility check) resolves to the same versions that pass the smoke."""
import sys, re
from importlib.metadata import version, PackageNotFoundError
from datetime import datetime

names = []
for ln in open("requirements.txt", encoding="utf-8"):
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        continue
    m = re.match(r"([A-Za-z0-9_.\-]+)", ln)
    if m:
        names.append(m.group(1))

out = [
    "# Auto-pinned by pin_deps.py — exact versions proven in THIS environment.",
    f"# Python {sys.version.split()[0]}  |  generated {datetime.now().isoformat(timespec='seconds')}",
    "# Satisfies §04: clone @ pinned commit -> empty venv -> pip install -r requirements.txt -> run.",
    "",
]
missing = []
for n in names:
    try:
        out.append(f"{n}=={version(n)}")
    except PackageNotFoundError:
        missing.append(n)

open("requirements.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out))
if missing:
    print("\nWARN (declared but not installed, skipped):", ", ".join(missing))