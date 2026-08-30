#!/usr/bin/env python3
"""
Similarity calculator for YANG node comparison.

This module provides utilities for calculating similarity between
YANG data structures to support intelligent matching and comparison.
"""

import json
from typing import Any, Dict, Set


class SimilarityCalculator:
    """Handles similarity calculations between nodes."""
    
    @staticmethod
    def flatten_for_comparison(data: Any, parent_key: str = '', sep: str = '.') -> Dict[str, str]:
        """
        Flatten nested data structure for stable comparison.
        
        Args:
            data: The data structure to flatten (dict, list, or primitive)
            parent_key: The parent key path for recursion
            sep: Separator character for key paths
            
        Returns:
            Flattened dictionary with path keys and JSON string values
        """
        if not isinstance(data, dict):
            try:
                return {parent_key: json.dumps(data, ensure_ascii=False, sort_keys=True)}
            except Exception:
                return {parent_key: str(data)}
        
        items = {}
        for k, v in data.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            
            if isinstance(v, dict):
                items.update(SimilarityCalculator.flatten_for_comparison(v, new_key, sep))
            elif isinstance(v, list):
                # Convert list to stable representation
                stable_list = []
                for item in v:
                    if isinstance(item, dict):
                        # Sort dict items for consistency
                        stable_list.append(json.dumps(item, sort_keys=True, default=str))
                    else:
                        stable_list.append(json.dumps(item, default=str))
                items[new_key] = json.dumps(tuple(stable_list), sort_keys=True, default=str)
            else:
                items[new_key] = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        
        return items
    
    @staticmethod
    def calculate_similarity(data1: Dict, data2: Dict, exclude_keys: Set[str] = None) -> float:
        """
        Calculate similarity percentage between two data structures.
        
        Args:
            data1: First data structure
            data2: Second data structure
            exclude_keys: Keys to exclude from comparison (e.g., 'name', 'path')
            
        Returns:
            Similarity percentage (0.0 to 100.0)
        """
        if exclude_keys is None:
            exclude_keys = {"name", "path"}
        
        flat1 = SimilarityCalculator.flatten_for_comparison(data1)
        flat2 = SimilarityCalculator.flatten_for_comparison(data2)
        
        # Remove excluded keys
        filtered1 = {k: v for k, v in flat1.items() if not any(ex in k for ex in exclude_keys)}
        filtered2 = {k: v for k, v in flat2.items() if not any(ex in k for ex in exclude_keys)}
        
        set1 = set(filtered1.items())
        set2 = set(filtered2.items())
        
        union = set1 | set2
        if not union:
            return 100.0
        
        intersection = set1 & set2
        return (len(intersection) / len(union)) * 100.0
