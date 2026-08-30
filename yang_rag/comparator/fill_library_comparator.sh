#!/bin/bash
# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run fill_yang_library.py with the correct path
python3 "${SCRIPT_DIR}/fill_yang_library.py" "$1"
python3 "${SCRIPT_DIR}/fill_yang_library.py" "$2"