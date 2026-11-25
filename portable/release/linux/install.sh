#!/usr/bin/env bash
# Sets up Frame Repair inside this folder: a private Python environment with PyTorch
# for your graphics card. Options are passed on, e.g.  ./install.sh --gpu cpu
set -euo pipefail
cd "$(dirname "$0")"

py=""
for c in python3.12 python3.13 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] < (3, 15) else 1)' 2>/dev/null; then
    py="$c"; break
  fi
done
if [ -z "$py" ]; then
  echo "Python 3.9 or newer is needed, e.g.: sudo apt install python3 python3-venv"
  exit 1
fi
echo "Python: $(command -v "$py") ($("$py" -V 2>&1))"

if [ ! -x app/.venv/bin/python ]; then
  echo "Creating the environment in app/.venv ..."
  if ! "$py" -m venv app/.venv; then
    rm -rf app/.venv
    echo "Creating the environment failed. On Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  fi
fi
app/.venv/bin/python app/tools/setup_env.py "$@"
echo
echo "Done. Try:  ./rife-repair clip.mov --dry-run     (./rife-repair --help for everything)"
