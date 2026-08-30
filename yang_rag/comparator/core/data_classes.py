#!/usr/bin/env python3
"""
Data classes for YANG comparison.

This module contains shared data structures used across the comparison system.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from .constants import ChangeType


@dataclass
class ChangeRecord:
    """Represents a single change in the comparison."""
    change_type: ChangeType  # Type-safe change type enum
    path: str
    node_type: str = "node"  # YANG structural like 'revision', 'grouping', 'leaf', etc.
    old_value: Any = None
    new_value: Any = None
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    details: List[Dict] = field(default_factory=list)
    
    def is_meaningful(self) -> bool:
        """Check if this change should be reported."""
        return self.change_type != ChangeType.UNCHANGED
    
    @property
    def is_added(self) -> bool:
        """Check if this is an addition."""
        return self.change_type == ChangeType.ADDED
    
    @property
    def is_deleted(self) -> bool:
        """Check if this is a deletion."""
        return self.change_type == ChangeType.DELETED
    
    @property
    def is_changed(self) -> bool:
        """Check if this is a modification."""
        return self.change_type == ChangeType.CHANGED
    
    @property
    def is_renamed(self) -> bool:
        """Check if this is a rename."""
        return self.change_type == ChangeType.RENAMED
