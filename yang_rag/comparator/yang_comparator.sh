#!/bin/bash
# YANG Comparator Pipeline Script
# Compares two YANG files and generates compatibility reports

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Get the project root (grandparent of comparator script)
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Run all scripts from PROJECT_ROOT to maintain consistent paths
cd "$PROJECT_ROOT"

# Parse arguments
OLD_FILE=""
NEW_FILE=""
OLD_DIR=""
NEW_DIR=""
EXTRA_FLAGS=""

# Parse positional args and flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --*)
      # Any flag starting with -- is treated as a compatibility flag
      EXTRA_FLAGS="$EXTRA_FLAGS $1"
      shift
      ;;
    *)
      # Positional arguments
      if [ -z "$OLD_FILE" ]; then
        OLD_FILE="$1"
      elif [ -z "$NEW_FILE" ]; then
        NEW_FILE="$1"
      elif [ -z "$OLD_DIR" ]; then
        OLD_DIR="$1"
      elif [ -z "$NEW_DIR" ]; then
        NEW_DIR="$1"
      fi
      shift
      ;;
  esac
done

# Validate required arguments
if [ -z "$OLD_FILE" ] || [ -z "$NEW_FILE" ] || [ -z "$OLD_DIR" ] || [ -z "$NEW_DIR" ]; then
  echo "Usage: $0 <old_file> <new_file> <old_dir> <new_dir> [--<flag>]"
  echo "  Examples: --rfc7950, --strict, --vendor-specific"
  exit 1
fi

# Ensure output directory exists
mkdir -p output

# Run the NEW modular comparator (yang_comparator.py with core modules)
python3 -m yang_rag.comparator.yang_comparator "$OLD_FILE" "$NEW_FILE" "$OLD_DIR" "$NEW_DIR"

# Run the rest of the pipeline scripts
python3 yang_rag/comparator/filter_report.py

# Pass full YANG file paths to enrichment for XPath resolution in condition analysis
OLD_YANG_PATH="$OLD_DIR/$OLD_FILE"
NEW_YANG_PATH="$NEW_DIR/$NEW_FILE"

# Pass flags to check_compatibility.py
python3 yang_rag/comparator/check_compatibility.py "$OLD_YANG_PATH" "$NEW_YANG_PATH" "$OLD_DIR" "$NEW_DIR" $EXTRA_FLAGS

# Export YANG file paths as environment variables for line number correction
export YANG_OLD_FILE="$OLD_YANG_PATH"
export YANG_NEW_FILE="$NEW_YANG_PATH"

python3 yang_rag/comparator/generate_compatibility_list.py
python3 yang_rag/comparator/generate_non_compatibility_list.py
python3 yang_rag/comparator/group_by_path.py --out output/final_report.json

# Group duplicate changes (same change in multiple locations due to grouping expansion)
python3 yang_rag/comparator/group_duplicate_changes.py --report output/final_report.json

