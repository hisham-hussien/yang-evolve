"""Backward-compatible type checker re-export module.

Legacy tests import from `yang_rag.comparator.yang_type_checker`.
The implementation now lives under `yang_rag.comparator.helper.yang_type_checker`.
"""

from yang_rag.comparator.helper.yang_type_checker import *  # noqa: F401,F403
from yang_rag.comparator.helper import yang_type_checker as _impl

# Legacy tests import this private symbol from the old module path.
_TYPEDEF_REGISTRY = _impl._TYPEDEF_REGISTRY
