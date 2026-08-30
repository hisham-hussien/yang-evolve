"""
setup.py — supplements pyproject.toml with data_files so that the data/
directory (pre-built RAG index + extracted corpus) is bundled into the wheel
and installed to <sys.prefix>/share/yang-comparator-pro/data/.

yang_rag/config.py resolves DATA_DIRECTORY by checking both:
  1. <project_root>/data/                              (editable / dev install)
  2. <sys.prefix>/share/yang-comparator-pro/data/     (wheel install)
"""
import os
from setuptools import setup

def _collect_data_files():
    """Walk data/ and return a list of (dest_dir, [src_files]) tuples."""
    data_files = []
    for dirpath, _dirnames, filenames in os.walk("data"):
        if not filenames:
            continue
        dest = os.path.join("share", "yang-comparator-pro", dirpath)
        src_files = [os.path.join(dirpath, f) for f in filenames]
        data_files.append((dest, src_files))
    return data_files

setup(data_files=_collect_data_files())
