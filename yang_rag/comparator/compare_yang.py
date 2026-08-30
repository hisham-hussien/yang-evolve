#!/usr/bin/env python3
# scripts/compare_yang.py
import sys
from compare_yang.comparator import main

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: scripts/compare_yang.py <yang_file1> <yang_file2> <modules_dir1> <modules_dir2>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
