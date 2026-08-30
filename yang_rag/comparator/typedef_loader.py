"""Backward-compatible typedef loader re-export module.

Legacy tests import from `yang_rag.comparator.typedef_loader`.
The implementation now lives under `yang_rag.comparator.helper.typedef_loader`.
"""

from yang_rag.comparator.helper.typedef_loader import *  # noqa: F401,F403
