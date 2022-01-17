#!/usr/bin/env bash

set -e -x
cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.."

python3.10 tools/test_py_version.py

python3.10 -m venv venv
source venv/bin/activate
python -c "import sys; assert sys.version_info[0:2] == (3, 10), 'Python 3.10 required'"
python -m pip install --upgrade -r requirements-setuptools.txt
python -m pip install --upgrade -r requirements-small.txt
python -m pip install -e . -e ".[gui]"

echo "Setup finished successfully."
