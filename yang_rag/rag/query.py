#!/usr/bin/env python3
"""
Query Engine for YANG-RAG
--------------------------
Query flow:
1. Parse & mask user query → build template
2. Embed query
3. Pre-filter by depth/category/path prefix
4. Vector top-K retrieval
5. Neighbor expansion from graph
6. Rerank with combined scoring
7. Return top-K with metadata
"""

import json
import numpy as np
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Import global RAG configuration
try:
    from yang_rag.config import RAG_INITIAL_K as _RAG_INITIAL_K
except Exception:
    _RAG_INITIAL_K = 3000  # fallback if config not available

# Optional BM25 (lexical ranking)
try:
    from rank_bm25 import BM25Okapi  # type: ignore
except Exception:
    BM25Okapi = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_DIR = PROJECT_ROOT / "data" / "index"

# Add parent to path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from embedding import EmbeddingModel, compute_similarity
from vector_store import FaissStore

# Import XML-driven keyword type mapping for masking
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from yang_rag.utils.yang_common import KEYWORD_XML_TYPES as _KEYWORD_XML_TYPES
    from yang_rag.utils.yang_common import XML_TYPE_TO_MASK as _XML_TYPE_TO_MASK
except Exception:
    _KEYWORD_XML_TYPES = {}
    _XML_TYPE_TO_MASK = {}

# Import pyang_extractor for sophisticated text-based parsing
from yang_rag.parsing.pyang_extractor import PyangYANGExtractor

console = Console()

# Paths
INDEX_DIR = PROJECT_ROOT / "data" / "index"


class YANGQuery:
    """Query engine with masking, embedding, and reranking."""
    
    # Keyword sets (copy from extractor)
    STRUCTURAL_KEYWORDS = {
        'module', 'submodule', 'container', 'leaf', 'list', 'leaf-list',
        'choice', 'case', 'grouping', 'typedef', 'rpc', 'notification',
        'action', 'anydata', 'anyxml', 'uses', 'augment', 'identity',
        'extension', 'feature', 'deviation', 'input', 'output', 'bit',
        'enum', 'type', 'import', 'include', 'belongs-to'
    }
    
    CONSTRAINT_KEYWORDS = {
        'must', 'when', 'pattern', 'range', 'length', 'unique',
        'mandatory', 'min-elements', 'max-elements', 'config',
        'ordered-by', 'presence', 'if-feature', 'fraction-digits',
        'require-instance', 'modifier', 'yin-element',
        'status'
    }
    
    ATTRIBUTE_KEYWORDS = {
        'description', 'reference', 'units', 'default', 'base',
        'namespace', 'prefix', 'yang-version', 'contact',
        'organization', 'revision', 'revision-date', 'argument',
        'value', 'position', 'error-message', 'error-app-tag',
        'path', 'refine', 'key'
    }
    
    ALL_KEYWORDS = STRUCTURAL_KEYWORDS | CONSTRAINT_KEYWORDS | ATTRIBUTE_KEYWORDS
    
    # Built-in YANG types (preserve these, don't mask)
    BUILTIN_TYPES = {
        'int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64',
        'decimal64', 'string', 'boolean', 'enumeration', 'bits', 'binary',
        'leafref', 'identityref', 'instance-identifier', 'empty', 'union'
    }
    
    # Keywords that take boolean arguments (for context-aware masking in any position)
    BOOLEAN_KEYWORDS = {'config', 'mandatory', 'ordered-by', 'yin-element', 'require-instance'}
    
    # Keywords that take number/range arguments
    NUMBER_KEYWORDS = {'range', 'length', 'min-elements', 'max-elements', 'fraction-digits', 'position', 'value'}
    
    # Scoring weights - MUST sum to 1.0 for proper normalization
    # 
    # Architecture: Clean separation between embeddings and explicit features
    # - Embeddings (structure_sim, description_sim): Semantic similarity of RAW snippets
    # - Explicit features: All structural/syntactic matching scored separately
    # - NO double-counting: embeddings don't include path/category/keywords/tokens
    WEIGHTS = {
        'structure_sim': 0.30,   # Semantic similarity of raw YANG snippet (neural embeddings)
        'description_sim': 0.05, # Semantic similarity of description text (for extensions)
        'path_shape': 0.10,      # Explicit structural path matching (module/container/leaf)
        'keyword_overlap': 0.05, # Explicit keyword matching (exact YANG keywords)
        'category_match': 0.05,  # Explicit category alignment (STRUCTURAL/ATTRIBUTE/CONSTRAINT)
        'neighbor_boost': 0.05,  # Context from neighbors (parent/child keywords)
        'lexical_overlap': 0.05, # Character-level n-gram overlap (typo tolerance, raw text)
        'bm25': 0.05,           # BM25 statistical text retrieval (rare term boost, raw text)
        'token_match': 0.30     # Explicit masked token matching (<STRING>, <LIST>, <REGEX>, etc.)
    }  # Total = 1.00
    
    # Note: Structure and description embeddings are separate for better granularity.
    # Template = raw snippet ONLY (no metadata), so embeddings capture pure semantic similarity
    # All structural features (path, keywords, tokens, category) scored explicitly - no conflicts!

    # ---------------------------------------------------------------------------
    # TOKEN_EQUIVALENTS: pairs of mask tokens treated as interchangeable for
    # multi-query RAG recall expansion.
    #
    # For each pair (A, B), when the primary query template contains token A,
    # a secondary query is run with token B substituted — and vice versa.
    # Results from all queries are merged, keeping the best score per keyword.
    #
    # Rules:
    #   - Both tokens must be interchangeable in the YANG masking context
    #   - Only add pairs where the value transformation is safe (no ambiguity)
    # ---------------------------------------------------------------------------
    TOKEN_EQUIVALENTS: tuple = (
        ('<STRING>', '<IDENTIFIER>'),   # quoted "lower" ↔ unquoted lower
        ('<RANGE>', '<LENGTH>'),        # future: range expressions
    )

    # Regex for values that are safe to unquote (simple identifiers only).
    # Excludes: paths (/foo/bar, ../foo), OIDs (1.3.6.1), ranges (1..10),
    #           dotted notation, multi-word strings, version strings.
    _SIMPLE_IDENTIFIER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_\-]*$')
    
    def __init__(self, index_dir: Path = INDEX_DIR, weights_override: Optional[Dict[str, float]] = None):
        self.index_dir = index_dir
        self.model = None
        self.embeddings = None
        self.metadata = []
        self.graph = {}
        self.extension_registry = set()
        self.node_index_map = {}
        self.vector_backend = 'numpy'
        self.faiss_store = None
        # BM25 state
        self.bm25_model = None
        self._bm25_scores_cache = None  # cache last query scores
        # Allow overriding base weights from CLI/config
        self.base_weights = dict(self.WEIGHTS)
        if weights_override:
            for k, v in weights_override.items():
                if k in self.base_weights:
                    try:
                        self.base_weights[k] = float(v)
                    except Exception:
                        pass
        
        # Initialize pyang extractor for sophisticated text-based parsing
        self.extractor = PyangYANGExtractor()
        
        self.load_index()

    # ------------------------------
    # Query analysis & utilities
    # ------------------------------
    def _tokenize_words(self, text: str) -> list:
        """Simple word tokenizer for lexical overlap (lowercase, alnum, length>=2)."""
        if not text:
            return []
        # Split on non-alphanumeric, keep words with at least 2 chars
        import re
        return [w for w in re.split(r"[^0-9A-Za-z_]+", text.lower()) if len(w) >= 2]

    def _lexical_overlap_score(self, qtext: str, dtext: str) -> float:
        """Compute lightweight lexical overlap between query text and doc raw snippet (Jaccard)."""
        qwords = set(self._tokenize_words(qtext))
        dwords = set(self._tokenize_words(dtext))
        if not qwords or not dwords:
            return 0.0
        inter = len(qwords & dwords)
        union = len(qwords | dwords)
        return inter / union if union > 0 else 0.0

    def _analyze_query_intent(self, query_text: str, query_meta: dict) -> dict:
        """Infer query intent to adapt weights dynamically."""
        text = (query_text or '').strip()
        is_extension = ':' in text.split()[0] if text else False
        has_value_tokens = any(t in query_meta.get('snippet_masked', '') for t in [
            '<STRING>', '<IDENTIFIER>', '<REGEX>', '<LIST>', '<NUMBER>', '<RANGE>', '<NO_VALUE>'
        ])
        kws = set(query_meta.get('keywords', []))
        is_structural_hint = bool(kws & self.STRUCTURAL_KEYWORDS)
        wants_constraint = bool(kws & self.CONSTRAINT_KEYWORDS)
        return {
            'is_extension': is_extension,
            'has_value_tokens': has_value_tokens,
            'is_structural_hint': is_structural_hint,
            'wants_constraint': wants_constraint
        }
    
    def load_index(self):
        """Load index, metadata, graph, and model."""
        console.print(f"\n[cyan]Loading index from:[/cyan]\n{self.index_dir}")
        
        # Load embeddings (both structure and description)
        emb_path = self.index_dir / "embeddings.npz"
        if not emb_path.exists():
            console.print(f"[red]Error: Index not found at {self.index_dir}[/red]")
            console.print("[yellow]Please run indexer first: python -m yang_rag.rag.indexer[/yellow]")
            sys.exit(1)
        
        data = np.load(emb_path)
        # Try new format first (with separate embeddings), fallback to old format
        if 'structure_embeddings' in data:
            self.embeddings = data['structure_embeddings']
            self.description_embeddings = data['description_embeddings']
            console.print(f"[green]✓ Loaded {self.embeddings.shape[0]} structure + description embeddings[/green]")
            console.print(f"[dim]  Structure: {self.embeddings.shape}, Description: {self.description_embeddings.shape}[/dim]")
        else:
            # Old format compatibility
            self.embeddings = data['embeddings']
            self.description_embeddings = None
            console.print(f"[green]✓ Loaded {self.embeddings.shape[0]} embeddings (legacy format)[/green]")
        
        # Load metadata
        meta_path = self.index_dir / "metadata.jsonl"
        with open(meta_path, 'r', encoding='utf-8') as f:
            self.metadata = [json.loads(line) for line in f]
        console.print(f"[green]✓ Loaded {len(self.metadata)} metadata records[/green]")
        # Build node_id → index map for quick context lookups
        self.node_index_map = {m['node_id']: i for i, m in enumerate(self.metadata)}

        # Initialize BM25 on searchable_text (alias of display_text)
        if BM25Okapi is not None:
            corpus_tokens = []
            for m in self.metadata:
                text = m.get('searchable_text') or m.get('display_text') or ''
                corpus_tokens.append(self._tokenize_words(text))
            try:
                self.bm25_model = BM25Okapi(corpus_tokens)
                console.print("[green]✓ Initialized BM25 (rank-bm25) over searchable_text[/green]")
            except Exception as e:
                console.print(f"[yellow]BM25 initialization failed: {e}[/yellow]")
                self.bm25_model = None
        
        # Load graph
        graph_path = self.index_dir / "graph.json"
        if graph_path.exists():
            with open(graph_path, 'r', encoding='utf-8') as f:
                self.graph = json.load(f)
            console.print(f"[green]✓ Loaded graph ({len(self.graph.get('nodes', {}))} nodes)[/green]")
        
        # Load model (enable GPU if available or requested via env)
        use_gpu = False
        try:
            import os
            import torch  # type: ignore
            env_use_gpu = os.getenv('RAG_USE_GPU') or os.getenv('USE_GPU') or ''
            if env_use_gpu.lower() in ('1', 'true', 'yes', 'cuda'):
                use_gpu = torch.cuda.is_available()
            else:
                # Auto-detect
                use_gpu = torch.cuda.is_available()
        except Exception:
            use_gpu = False

        model_dir = self.index_dir / "model"
        self.model = EmbeddingModel(use_gpu=use_gpu)
        # Align TF-IDF fallback dimension (if used) with loaded embeddings
        expected_dim = int(self.embeddings.shape[1]) if self.embeddings is not None else None
        self.model.load(model_dir, expected_dim=expected_dim)
        # If the model directory was missing, persist the loaded model for next runs
        try:
            if not model_dir.exists() and self.model.model_type == 'sentence-transformer':
                model_dir.mkdir(parents=True, exist_ok=True)
                self.model.save(model_dir)
                console.print(f"[dim]Cached embedding model at {model_dir} for faster future loads[/dim]")
        except Exception:
            pass
        
        # Load stats to detect vector backend
        stats_path = self.index_dir / 'index_stats.json'
        if stats_path.exists():
            try:
                with open(stats_path, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
                    self.vector_backend = stats.get('vector_backend', 'numpy')
            except Exception:
                self.vector_backend = 'numpy'
        
        # Initialize FAISS if available/selected (load both structure and description indices)
        if self.vector_backend == 'faiss':
            try:
                self.faiss_store = FaissStore(dim=int(self.embeddings.shape[1]))
                self.faiss_store.load(self.index_dir, prefix='structure_')
                console.print("[green]✓ Loaded structure FAISS index[/green]")
                
                if self.description_embeddings is not None:
                    self.description_faiss_store = FaissStore(dim=int(self.description_embeddings.shape[1]))
                    self.description_faiss_store.load(self.index_dir, prefix='description_')
                    console.print("[green]✓ Loaded description FAISS index[/green]")
                else:
                    self.description_faiss_store = None
            except Exception as e:
                console.print(f"[yellow]FAISS not available or failed to load: {e}. Falling back to NumPy search.[/yellow]")
                self.vector_backend = 'numpy'
        
        # Build extension registry
        for meta in self.metadata:
            self.extension_registry.update(meta.get('extension_imports', []))
        
        console.print(f"[dim]  Extension registry: {len(self.extension_registry)} extensions[/dim]")
    
    def tokenize(self, text: str) -> List[str]:
        """Tokenize YANG snippet, preserving quoted strings and range expressions."""
        # Pattern priorities (order matters!):
        # 1. Quoted strings
        # 2. Version patterns (e.g., 0.1.0, 2.5.3-beta) - BEFORE range patterns
        # 3. Range expressions (e.g., 1..100, 1..100|106|108)
        # 4. Identifiers/keywords
        # 5. Simple integers
        # 6. Special characters
        pattern = r'"[^"]*"|\'[^\']*\'|\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9]+)?|[\d\-]+(?:\.\.[\d\-]+)?(?:\|[\d\-]+)*|[a-zA-Z_][\w\-.:]*|\d+|[{};,\[\]\(\)]'
        return re.findall(pattern, text)
    
    def is_regex_pattern(self, token: str) -> bool:
        """Check if token looks like a regex pattern."""
        # Heuristic: contains regex metacharacters
        regex_chars = ['^', '$', '*', '+', '?', '[', ']', '|', '(', ')', '.', '\\']
        return any(c in token for c in regex_chars)
    
    def mask_token(self, token: str, context: str = '', prev_token: str = '') -> str:
        """
        Mask a token per rules with context awareness.
        
        Args:
            token: The token to mask
            context: Optional context ('pattern', 'boolean', 'number', 'type') to guide masking
            prev_token: Previous token for context detection
        """
        # Preserve keywords
        if token in self.ALL_KEYWORDS:
            return token
        
        # Preserve built-in YANG types
        if token in self.BUILTIN_TYPES:
            return token
        
        # Handle prefixed tokens (extensions and typedefs)
        if ':' in token:
            parts = token.split(':', 1)
            if len(parts) == 2:
                prefix, name = parts
                # In type context, prefixed names are typedef references (e.g., oc-yang:counter64)
                if prev_token == 'type' or context == 'type':
                    return '<TYPEDEF>'
                # Preserve ALL vendor extension keywords (prefixed tokens in statement position).
                # Previously only preserved if name was in extension_registry, but vendor extensions
                # (ext:operation-exclude, tailf:callpoint, smiv2:oid, sros-ext:immutable, etc.)
                # are NOT in the registry because they come from files not indexed.
                # Keeping the full prefixed name is critical for token_match scoring.
                if name in self.extension_registry:
                    return token
                # Preserve any prefixed token that looks like a vendor extension keyword
                # (hyphenated names, or names not in standard YANG keyword sets)
                if name not in self.ALL_KEYWORDS and name not in self.BUILTIN_TYPES:
                    return token  # preserve: e.g. ext:operation-exclude, tailf:callpoint
        
        # Detect context from previous token (for any position, not just second token)
        if prev_token in self.BOOLEAN_KEYWORDS:
            if token in ['true', 'false']:
                return '<BOOLEAN>'
        
        if prev_token in self.NUMBER_KEYWORDS:
            # Check for range pattern (only for range/length keywords)
            if prev_token in ['range', 'length']:
                content = token.strip('"').strip("'")
                # Range pattern: digits with .. or | separators
                if re.match(r'^[\d\-]+\.\.[\d\-]+', content) or (re.match(r'^[\d\.\-|]+$', content) and '|' in content):
                    return '<RANGE>'
            # Numeric values
            if token.startswith('"') or token.startswith("'"):
                return '<NUMBER>'
            if re.match(r'^[\d\.\-]+$', token):
                return '<NUMBER>'
            try:
                float(token)
                return '<NUMBER>'
            except:
                pass
        
        # Context-aware masking (for initial context detection)
        if context == 'boolean':
            if token in ['true', 'false']:
                return '<BOOLEAN>'
        
        if context == 'number':
            content = token.strip('"').strip("'")
            # Range pattern detection (only numeric ranges)
            if re.match(r'^[\d\-]+\.\.[\d\-]+', content) or (re.match(r'^[\d\.\-|]+$', content) and '|' in content):
                return '<RANGE>'
            # Numeric values
            if token.startswith('"') or token.startswith("'"):
                return '<NUMBER>'
            if re.match(r'^[\d\.\-]+$', token):
                return '<NUMBER>'
            try:
                float(token)
                return '<NUMBER>'
            except:
                pass
        
        # Regex pattern context
        if context == 'pattern' or prev_token == 'pattern':
            if token.startswith('"') or token.startswith("'"):
                return '<REGEX>'
        
        # Mask quoted strings (description, etc.)
        if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
            content = token[1:-1]
            # Range patterns like "1..100" or "1..100|106"
            if re.match(r'^[\d\-]+\.\.[\d\-]+', content) or (re.match(r'^[\d\.\-|]+$', content) and '|' in content):
                return '<RANGE>'
            # In type context, quoted prefixed names are typedef references (e.g., "oc-yang:counter64")
            if (prev_token == 'type' or context == 'type') and ':' in content:
                return '<TYPEDEF>'
            # Pattern test extensions use test values
            if prev_token and ('pattern-test-pass' in prev_token or 'pattern-test-fail' in prev_token):
                if self.is_mac_address(content):
                    return '<MAC_ADDRESS>'
                if self.is_ip_address(content):
                    return '<IP_ADDRESS>'
                return '<REGEX>'
            # Boolean values: true/false
            if content.lower() in ('true', 'false'):
                return '<BOOLEAN>'
            # Space-separated list (unique keys, multi-value, human-readable strings): <LIST>
            # Applies to any multi-word quoted value — covers YANG key lists, multi-value enums,
            # and human-readable description strings (all are multi-token, not single-value).
            if ' ' in content:
                return '<LIST>'
            # XPath/path expressions:
            #   - absolute: /foo/bar
            #   - relative: ../foo, ./foo, ../../foo/bar
            #   - dotted path segments: foo/bar/baz
            if (content.startswith('/')
                    or content.startswith('../')
                    or content.startswith('./')
                    or re.match(r'^[\w:\-]+(?:/[\w:\-]+)+$', content)):
                return '<PATH>'
            # SNMP OIDs: all-numeric dotted notation with 4+ segments (1.3.6.1.2.1.96)
            if re.match(r'^\d+(?:\.\d+){3,}$', content):
                return '<STRING>'
            # Pure numeric values (integers, decimals like 3.5, 45)
            if re.match(r'^\d+(?:\.\d+)?$', content):
                return '<NUMBER>'
            # Version strings: exactly 3 numeric segments (1.0.2) or release tags (rel25)
            if re.match(r'^\d+\.\d+\.\d+(?:[.\-][a-zA-Z0-9]+)?$', content):
                return '<VERSION>'
            if re.match(r'^rel\d+(?:\.\d+)*$', content):
                return '<VERSION>'
            # XML-driven masking: if prev_token has a known XML type, use it.
            # Handles: when/must -> <CONDITION>, yang-version -> <VERSION>,
            # key/unique -> <LIST>, yin-element -> <BOOLEAN>, etc.
            if prev_token and prev_token in _KEYWORD_XML_TYPES:
                xml_type = _KEYWORD_XML_TYPES[prev_token]
                if xml_type:
                    return _XML_TYPE_TO_MASK.get(xml_type, '<STRING>')
            return '<STRING>'
        
        # Check for unquoted range patterns (e.g., 0..0, 1..10, -10..10)
        if re.match(r'^[\d\-]+\.\.[\d\-]+', token):
            return '<RANGE>'
        
        # Check for version patterns (e.g., 0.1.0, 2.5.3, 2023.10.15)
        if re.match(r'^\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9]+)?$', token):
            return '<VERSION>'

        # XML-driven masking for unquoted tokens (e.g. yang-version 1 -> <VERSION>).
        # Must run BEFORE the generic float() check so that keywords whose XML type
        # is 'version' (or any other non-number type) are not incorrectly masked as <NUMBER>.
        if prev_token and prev_token in _KEYWORD_XML_TYPES:
            xml_type = _KEYWORD_XML_TYPES[prev_token]
            if xml_type:
                return _XML_TYPE_TO_MASK.get(xml_type, '<STRING>')

        # Mask unquoted numbers (simple integers/floats)
        try:
            float(token)
            return '<NUMBER>'
        except:
            pass
        
        # Regex pattern heuristic (for unquoted patterns)
        if any(c in token for c in ['^', '$', '*', '+', '?', '[', ']', '\\']):
            return '<REGEX>'

        # Boolean values (unquoted true/false — common in extension arguments)
        if token.lower() in ('true', 'false'):
            return '<BOOLEAN>'

        # Mask identifiers (unquoted names)
        if token not in ['{', '}', ';', ',', '(', ')', '[', ']']:
            return '<IDENTIFIER>'
        
        return token
    
    def is_mac_address(self, value: str) -> bool:
        """Check if a value is a valid MAC address (EUI-48 standard: 6 octets)."""
        # Valid MAC address formats:
        # - 6 groups of exactly 2 hex digits with : or - separators (00:1A:2B:3C:4D:5E)
        # - 12 hex digits without separators (001A2B3C4D5E - Cisco style)
        # - Case-insensitive hex digits only
        
        # Colon or dash-separated format: 6 groups of 2 hex digits
        if re.match(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$', value):
            return True
        
        # No separators: exactly 12 hex digits (Cisco style)
        if re.match(r'^[0-9A-Fa-f]{12}$', value):
            return True
        
        return False
    
    def is_ip_address(self, value: str) -> bool:
        """Check if a value is a valid IPv4 or IPv6 address."""
        # IPv4: 192.168.1.1 (4 octets, each 0-255)
        # IPv6: 2001:db8::1, ::1, fe80::1 (compressed format supported)
        # Both support optional zone/interface suffix (e.g., %eth0)
        
        # IPv4 pattern with strict octet validation (0-255)
        ipv4_pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(%[a-zA-Z0-9\-_]+)?$'
        if re.match(ipv4_pattern, value):
            return True
        
        # IPv6 pattern - must have at least one group with 3-4 hex digits OR contain ::
        # This prevents short hex values like "0:a:0:9:0:d:1:0" from matching
        # Valid: 2001:db8::1, fe80::1, ::1, 2001:0db8:0000:0000:0000:ff00:0042:8329
        # Invalid: 0:a:0:9 (too short), 11:11:11:11 (looks like MAC with wrong count)
        if '::' in value or any(len(part) >= 3 for part in value.split(':') if part and '%' not in part):
            ipv6_pattern = r'^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(%[a-zA-Z0-9\-_]+)?$'
            return bool(re.match(ipv6_pattern, value))
        
        return False
    
    def mask_snippet(self, text: str) -> str:
        """Mask YANG snippet using pyang_extractor's sophisticated context-aware masking.
        
        Uses extractor.mask_snippet() which:
        - Detects statement context (type, pattern, key, name, boolean, number, extension)
        - Handles typedef references and locally defined types
        - Context-aware masking for different statement types
        - Properly handles extension statements with and without values
        """
        # Note: pyang_extractor's mask_snippet takes optional module_name parameter
        # We don't have module context at query time, so we pass empty string
        return self.extractor.mask_snippet(text, module_name='')
    
    def detect_keywords(self, text: str) -> List[str]:
        """Detect keywords using pyang_extractor's sophisticated detection.
        
        Uses extractor.detect_keywords_in_snippet() which:
        - Detects ALL keywords in statement positions (not just first token)
        - Handles extensions (prefixed and unprefixed)
        - Identifies unknown extension keywords
        - Ignores keywords in non-statement positions (e.g., leaf names)
        """
        return self.extractor.detect_keywords_in_snippet(text)
    
    def build_skeleton_path_from_snippet(self, text: str, yang_file: Optional[str] = None) -> str:
        """
        Build skeleton path using EXACT same logic as pyang_extractor.
        
        If yang_file is provided:
        1. Parse the full YANG file to get complete AST
        2. Find the statement matching our snippet in the AST
        3. Use extractor's build_keyword_skeleton_path() to traverse UP the tree
        
        This gives us accurate paths like:
        - "prefix oc-ext;" in import -> "module/import/prefix"
        - "prefix ex;" at module level -> "module/prefix"  
        - "pattern '[0-9]+';" in type -> "module/leaf/type/pattern"
        
        Args:
            text: YANG snippet from query
            yang_file: Path to the original YANG file containing this snippet
        
        Returns:
            Accurate skeleton path from AST traversal
        """
        if not yang_file:
            # Fallback if no file provided - use the extension keyword name directly.
            # For vendor extensions, the keyword itself is the most meaningful path component.
            keywords = self.detect_keywords(text)
            if keywords:
                ext_kw = keywords[0]
                return f'module/{ext_kw}'
            return 'module'
        
        try:
            from pyang import context, repository
            import os
            
            # Parse the full YANG file
            yang_path = str(yang_file)
            if not os.path.exists(yang_path):
                # File doesn't exist, use keyword-based fallback
                keywords = self.detect_keywords(text)
                return f'module/{keywords[0]}' if keywords else 'module'
            
            repos = repository.FileRepository(os.path.dirname(yang_path) or '.')
            ctx = context.Context(repos)
            
            with open(yang_path, 'r', encoding='utf-8') as f:
                file_content = f.read()
            
            module = ctx.add_module(yang_path, file_content)
            if module is None:
                # Parse failed, use keyword-based fallback
                keywords = self.detect_keywords(text)
                return f'module/{keywords[0]}' if keywords else 'module'
            
            # Find the statement in the AST that matches our snippet.
            # For extension bare queries (first token is a prefixed keyword like tailf:actionpoint),
            # ONLY match nodes whose keyword matches the first token of the snippet.
            # This prevents sub-statements (e.g. tailf:validate inside tailf:actionpoint)
            # from being matched and producing wrong paths.
            import re as _re
            snippet_first_token = text.strip().split()[0] if text.strip() else ''
            snippet_first_base = snippet_first_token.split(':', 1)[1] if ':' in snippet_first_token else snippet_first_token
            is_extension_query = ':' in snippet_first_token

            def _keyword_matches_snippet_token(node_keyword: str, base_node_keyword: str,
                                               snippet_clean: str) -> bool:
                """
                Return True only when the node's keyword appears as a *statement keyword*
                in the snippet — not merely as a substring of an identifier or module name.

                Rules:
                  - Prefixed form (e.g. ``smiv2:defval``): require the full prefixed token
                    to appear at a word boundary followed by whitespace, ``;``, or ``{``.
                  - Bare form (e.g. ``defval``): require the keyword to appear at the start
                    of the stripped snippet OR after whitespace, followed by whitespace/``;``/``{``.
                    This prevents ``defval`` from matching ``submodule demo-defval {``.
                """
                # Prefixed form: smiv2:defval, oc-ext:telemetry-on-change, etc.
                if ':' in node_keyword:
                    return bool(re.search(
                        r'(?<![:\w])' + re.escape(node_keyword) + r'(?=[\s;{]|$)',
                        snippet_clean
                    ))
                # Bare keyword: must appear as a statement token, not inside an identifier
                # e.g. "defval" must NOT match "demo-defval" or "submodule demo-defval {"
                return bool(re.search(
                    r'(?:^|(?<=\s))' + re.escape(node_keyword) + r'(?=[\s;{]|$)',
                    snippet_clean
                ))

            def find_all_matching_statements(node, snippet_text, matches=None):
                """Recursively find all statements matching the snippet."""
                if matches is None:
                    matches = []
                
                # Get the raw representation of this statement
                node_keyword = self.extractor.get_keyword_string(node)
                node_arg = getattr(node, 'arg', None)

                # For extension bare queries (first token is a prefixed keyword like tailf:actionpoint),
                # skip nodes whose base keyword does NOT match the first token of the snippet.
                # This prevents sub-statements (e.g. tailf:validate inside tailf:actionpoint block)
                # from being matched and producing wrong skeleton paths.
                base_node_keyword = node_keyword.split(':', 1)[1] if ':' in node_keyword else node_keyword
                if is_extension_query:
                    if base_node_keyword != snippet_first_base and node_keyword != snippet_first_token:
                        # Recurse into children but skip this node
                        if hasattr(node, 'substmts'):
                            for substmt in node.substmts:
                                find_all_matching_statements(substmt, snippet_text, matches)
                        return matches

                # Extract the argument value from snippet (between quotes or first word after keyword)
                import re
                snippet_clean = snippet_text.strip()
                
                # Check for exact match
                is_match = False
                
                if node_arg:
                    # For statements with arguments, match if:
                    # 1. The keyword matches as a statement token (not as part of an identifier)
                    # 2. The argument starts the same way (for long regex/xpath, don't require exact match)
                    
                    # Check if keyword appears as a statement in the snippet (not as part of a name)
                    keyword_in_snippet = _keyword_matches_snippet_token(
                        node_keyword, base_node_keyword, snippet_clean
                    )
                    
                    if keyword_in_snippet:
                        # Try to extract the argument from snippet
                        # Pattern 1: keyword "arg" or keyword 'arg' (try both full and base keyword)
                        quoted_match = re.search(rf'{re.escape(node_keyword)}\s+["\']([^"\']+)["\']', snippet_clean)
                        if not quoted_match and ':' in node_keyword:
                            # Try base keyword without prefix
                            quoted_match = re.search(rf'{re.escape(base_node_keyword)}\s+["\']([^"\']+)["\']', snippet_clean)
                        
                        # Pattern 2: keyword arg; (unquoted)
                        unquoted_match = re.search(rf'{re.escape(node_keyword)}\s+([^\s;{{]+)', snippet_clean)
                        if not unquoted_match and ':' in node_keyword:
                            # Try base keyword without prefix
                            unquoted_match = re.search(rf'{re.escape(base_node_keyword)}\s+([^\s;{{]+)', snippet_clean)
                        
                        snippet_arg = None
                        if quoted_match:
                            snippet_arg = quoted_match.group(1)
                        elif unquoted_match:
                            snippet_arg = unquoted_match.group(1)
                        
                        # Match if: exact match OR argument starts the same (for long patterns)
                        # This handles regex patterns and XPath expressions that might be truncated
                        if snippet_arg:
                            # For long arguments (>20 chars), match on prefix only
                            if len(snippet_arg) > 20 and len(node_arg) > 20:
                                # Match if first 20 characters are the same
                                if snippet_arg[:20] == node_arg[:20]:
                                    is_match = True
                            # For shorter arguments, require exact match
                            elif snippet_arg == node_arg:
                                is_match = True
                        else:
                            # No argument found in snippet, but keyword matches
                            # This happens when snippet is "regexp-posix;" but AST has "oc-ext:regexp-posix 'value'"
                            # Match on keyword alone if it's an extension (has prefix in node)
                            if ':' in node_keyword:
                                is_match = True
                else:
                    # Statement without argument - match only when keyword appears as a statement token
                    # (not as part of an identifier/module name like "demo-defval")
                    if _keyword_matches_snippet_token(node_keyword, base_node_keyword, snippet_clean):
                        is_match = True
                
                if is_match:
                    # Calculate match quality score (prefer deeper/more specific matches)
                    depth = 0
                    current = node
                    while hasattr(current, 'parent') and current.parent:
                        depth += 1
                        current = current.parent
                    
                    matches.append((node, depth))
                
                # Recursively search children
                if hasattr(node, 'substmts'):
                    for substmt in node.substmts:
                        find_all_matching_statements(substmt, snippet_text, matches)
                
                return matches
            
            # Find all matching statements (with extension keyword filter for extension queries)
            matches = find_all_matching_statements(module, text)

            # If extension filter produced no matches, retry without the filter
            # (e.g. SMIv2 extensions where AST keyword representation differs)
            if not matches and is_extension_query:
                saved = is_extension_query
                is_extension_query = False
                matches = find_all_matching_statements(module, text)
                is_extension_query = saved

            if matches:
                # Pick the best match (deepest/most specific)
                # Sort by depth (descending) - deeper matches are more specific
                matches.sort(key=lambda x: x[1], reverse=True)
                best_stmt = matches[0][0]
                
                # Use extractor's EXACT path building logic
                # (Now removes last segment for better generalization)
                path = self.extractor.build_keyword_skeleton_path(best_stmt)
                return path
            
            # No match found in AST — use keyword-based fallback.
            # For vendor extensions (bare query snippets), the extension keyword
            # itself is the most meaningful path component.
            keywords = self.detect_keywords(text)
            if keywords:
                ext_kw = keywords[0]
                return f'module/{ext_kw}'
            return 'module'
            
        except Exception as e:
            # Any error, use keyword-based fallback
            keywords = self.detect_keywords(text)
            if keywords:
                return f'module/{keywords[0]}'
            return 'module'
    
    def determine_category(self, keywords: List[str], snippet: str = '') -> str:
        """
        Determine category from keywords and snippet structure.
        
        For extensions (unknown keywords), categorize based on structure:
        - Extension with block {} → STRUCTURAL
        - Extension with value only → "ATTRIBUTE, CONSTRAINT"
        """
        keywords_set = set(keywords)
        
        # Check if this is an extension (unknown keyword or in extension registry)
        unknown_keywords = keywords_set - (self.STRUCTURAL_KEYWORDS | 
                                          self.CONSTRAINT_KEYWORDS | 
                                          self.ATTRIBUTE_KEYWORDS)
        
        is_extension = False
        if unknown_keywords:
            is_extension = True
        elif any(kw in self.extension_registry for kw in keywords_set):
            is_extension = True
        
        # If it's an extension, categorize based on structure
        if is_extension:
            # Check if snippet has a YANG block (not just any { })
            # YANG blocks have specific format: keyword arg { substmt; substmt; }
            # Regex patterns have { } for quantifiers like {2} or {0,3}
            # Look for YANG block pattern: { followed by keywords/statements and closing }
            # Simple heuristic: if we have quoted strings or regex patterns, it's not a block
            has_quoted = ('"' in snippet or "'" in snippet)
            has_semicolon_before_brace = ';' in snippet and snippet.index(';') < snippet.index('{') if '{' in snippet else False
            
            # A real YANG block has: keyword arg { ... }
            # NOT: keyword "value{2}";
            # Determine if snippet contains a real YANG block { ... } vs braces inside a value.
            # Rule: structural extensions always open a block; value/attribute extensions never do.
            # A real YANG block has { and } that are NOT inside a quoted string.
            has_block = False
            if '{' in snippet and '}' in snippet:
                # Walk the snippet character by character to find unquoted { }
                in_quote = False
                quote_char = None
                unquoted_open = 0
                for ch in snippet:
                    if not in_quote and ch in ('"', "'"):
                        in_quote = True
                        quote_char = ch
                    elif in_quote and ch == quote_char:
                        in_quote = False
                    elif not in_quote and ch == '{':
                        unquoted_open += 1
                has_block = unquoted_open > 0
            
            if has_block:
                return 'STRUCTURAL'
            else:
                # Extension with value → "ATTRIBUTE, CONSTRAINT"
                return 'ATTRIBUTE, CONSTRAINT'
        
        # For known keywords, use pre-defined categories directly
        if any(kw in self.STRUCTURAL_KEYWORDS for kw in keywords_set):
            return 'STRUCTURAL'
        if any(kw in self.CONSTRAINT_KEYWORDS for kw in keywords_set):
            return 'CONSTRAINT'
        if any(kw in self.ATTRIBUTE_KEYWORDS for kw in keywords_set):
            return 'ATTRIBUTE'
        
        # Default for unknown (not extension, not in keyword sets)
        return 'ATTRIBUTE, CONSTRAINT'
        
    def build_query_template(self, query_text: str, yang_file: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """Build query template from raw query text.
        
        Template: Raw query text only (no path, no category, no keywords)
        
        This ensures clean separation:
        - Embeddings capture semantic similarity of raw YANG code
        - Path, category, keywords, and tokens are scored explicitly (no double-counting)
        
        Args:
            query_text: YANG snippet
            yang_file: Optional path to full YANG file (for accurate path extraction)
        """
        # Mask (for metadata and explicit scoring)
        masked = self.mask_snippet(query_text)
        
        # Detect keywords (for metadata and explicit scoring)
        keywords = self.detect_keywords(query_text)
        
        # Build skeleton path using extractor's logic (with optional YANG file, for explicit scoring)
        skeleton_path = self.build_skeleton_path_from_snippet(query_text, yang_file)
        
        # Determine category (pass query_text to check for block structure, for explicit scoring)
        category = self.determine_category(keywords, query_text)
        
        # Build template: RAW QUERY TEXT ONLY
        # This prevents double-counting in embeddings vs explicit feature scoring
        template = masked
        
        metadata = {
            'skeleton_path': skeleton_path,
            'category': category,
            'keywords': keywords,
            'snippet_masked': masked,
            'raw_query': query_text
        }
        
        return template, metadata
    
    def prefilter(self, query_meta: Dict[str, Any], 
                  depth_window: int = 2,
                  category_filter: Optional[str] = None) -> np.ndarray:
        """
        Pre-filter candidates by depth/category/path.
        Returns boolean mask of valid candidates.
        """
        mask = np.ones(len(self.metadata), dtype=bool)
        
        # Category filter with support for "ATTRIBUTE, CONSTRAINT"
        if category_filter:
            for i, meta in enumerate(self.metadata):
                doc_cat = meta['category']
                
                # If query is "ATTRIBUTE, CONSTRAINT", match ATTRIBUTE, CONSTRAINT, or "ATTRIBUTE, CONSTRAINT"
                if category_filter == 'ATTRIBUTE, CONSTRAINT':
                    if doc_cat not in ['ATTRIBUTE', 'CONSTRAINT', 'ATTRIBUTE, CONSTRAINT']:
                        mask[i] = False
                # If query is ATTRIBUTE or CONSTRAINT, also match "ATTRIBUTE, CONSTRAINT"
                elif category_filter in ['ATTRIBUTE', 'CONSTRAINT']:
                    if doc_cat != category_filter and doc_cat != 'ATTRIBUTE, CONSTRAINT':
                        mask[i] = False
                # Otherwise exact match
                else:
                    if doc_cat != category_filter:
                        mask[i] = False
        
        # Path prefix filter (if query has specific structure)
        q_path = query_meta['skeleton_path']
        if q_path and q_path != 'module':
            for i, meta in enumerate(self.metadata):
                if not mask[i]:
                    continue
                # Check if paths share prefix
                doc_path = meta['skeleton_path']
                if not doc_path.startswith(q_path.split('/')[0]):
                    mask[i] = False
        
        return mask
    
    def vector_search(self, query_template: str,
                     prefilter_mask: Optional[np.ndarray] = None,
                     top_k: int = 20) -> List[int]:
        """Vector search with optional pre-filtering.
        Uses FAISS if available, otherwise falls back to NumPy cosine search.
        """
        # For extension queries (vendor-prefixed keyword like oc-ext:openconfig-version),
        # strip the vendor prefix before embedding so the vector search finds standard
        # YANG keywords with the same mask token (e.g. yang-version <VERSION>).
        # The full prefixed template is still used for token matching and reranking.
        embed_template = query_template
        first_token = query_template.strip().split()[0] if query_template.strip() else ''
        if ':' in first_token:
            # Strip prefix: 'oc-ext:openconfig-version <VERSION> ;' -> 'openconfig-version <VERSION> ;'
            base_name = first_token.split(':', 1)[1]
            embed_template = query_template.replace(first_token, base_name, 1)

        # Embed query
        query_vec = self.model.encode(embed_template, show_progress=False).astype('float32')

        if self.vector_backend == 'faiss' and self.faiss_store is not None:
            # Search a larger pool, then post-filter by mask if provided
            search_k = max(top_k * 5, top_k + 200)
            res = self.faiss_store.search(query_vec, top_k=search_k)
            cand_indices = res.indices
            # Post-filter
            if prefilter_mask is not None:
                valid = np.where(prefilter_mask)[0]
                valid_set = set(int(v) for v in valid)
                filtered = [int(i) for i in cand_indices if int(i) in valid_set]
                return filtered[:top_k]
            return cand_indices[:top_k].tolist()
        
        # NumPy fallback
        if prefilter_mask is not None:
            valid_indices = np.where(prefilter_mask)[0]
            if len(valid_indices) == 0:
                return []
            valid_embeddings = self.embeddings[valid_indices]
        else:
            valid_indices = np.arange(len(self.embeddings))
            valid_embeddings = self.embeddings

        sims = compute_similarity(query_vec, valid_embeddings, metric='cosine')
        top_k = min(top_k, len(sims))
        # kind='stable' ensures deterministic order when scores are tied across platforms
        top_indices_in_valid = np.argsort(sims, kind='stable')[-top_k:][::-1]
        top_indices = valid_indices[top_indices_in_valid]
        return top_indices.tolist()
    
    def expand_neighbors(self, node_ids: List[str]) -> Dict[str, Any]:
        """Expand neighbors from graph."""
        neighbor_info = {}
        
        nodes = self.graph.get('nodes', {})
        
        for node_id in node_ids:
            if node_id not in nodes:
                continue
            
            node = nodes[node_id]
            neighbors = []
            
            # Get parent
            if node.get('parent_id'):
                parent_id = node['parent_id']
                if parent_id in nodes:
                    neighbors.append({
                        'id': parent_id,
                        'type': 'parent',
                        'keywords': nodes[parent_id].get('keywords', [])
                    })
            
            # Get children (find nodes with this as parent)
            for nid, n in nodes.items():
                if n.get('parent_id') == node_id:
                    neighbors.append({
                        'id': nid,
                        'type': 'child',
                        'keywords': n.get('keywords', [])
                    })
            
            neighbor_info[node_id] = neighbors
        
        return neighbor_info
    
    def compute_path_shape_score(self, path1: str, path2: str) -> float:
        """Compute path shape similarity (normalized prefix match)."""
        parts1 = path1.split('/')
        parts2 = path2.split('/')
        
        # Count matching prefix
        match_count = 0
        for p1, p2 in zip(parts1, parts2):
            if p1 == p2:
                match_count += 1
            else:
                break
        
        max_len = max(len(parts1), len(parts2))
        if max_len == 0:
            return 0.0
        
        return match_count / max_len
    
    def compute_keyword_overlap_score(self, kws1: List[str], kws2: List[str]) -> float:
        """Compute keyword overlap (Jaccard)."""
        set1 = set(kws1)
        set2 = set(kws2)
        
        if not set1 or not set2:
            return 0.0
        
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        
        return intersection / union if union > 0 else 0.0
    
    def rerank(self, candidate_indices: List[int],
               query_meta: Dict[str, Any],
               query_structure_vec: np.ndarray,
               query_description_vec: Optional[np.ndarray] = None) -> List[Tuple[int, float, Dict[str, float]]]:
        """
        Rerank candidates with combined scoring (structure + description similarity).
        Returns: [(index, final_score, score_components), ...]
        """
        results = []
        # Get neighbor info
        node_ids = [self.metadata[i]['node_id'] for i in candidate_indices]
        neighbor_info = self.expand_neighbors(node_ids)
        
        # BM25 scores for all docs (if available), then normalize across candidates
        bm25_scores_all = None
        if self.bm25_model is not None:
            try:
                qtokens = self._tokenize_words(query_meta.get('raw_query', '') or '')
                bm25_scores_all = np.array(self.bm25_model.get_scores(qtokens), dtype=float)
            except Exception:
                bm25_scores_all = None

        # Precompute normalization stats for BM25 over candidates
        bm25_max = 0.0
        if bm25_scores_all is not None and candidate_indices:
            cand_vals = bm25_scores_all[candidate_indices]
            bm25_max = float(np.max(cand_vals)) if cand_vals.size else 0.0

        for idx in candidate_indices:
            meta = self.metadata[idx]
            
            # Structure similarity (always computed)
            doc_structure_vec = self.embeddings[idx:idx+1]
            structure_sim = compute_similarity(query_structure_vec, doc_structure_vec, metric='cosine')[0]
            
            # Description similarity
            # If doc has no description, boost to 1.0 (shouldn't penalize for missing description)
            description_sim = 1.0
            if query_description_vec is not None and self.description_embeddings is not None:
                doc_description_vec = self.description_embeddings[idx:idx+1]
                # Only compute similarity if doc has non-zero description embedding
                if np.any(doc_description_vec):
                    description_sim = compute_similarity(query_description_vec, doc_description_vec, metric='cosine')[0]
                # else: keep description_sim = 1.0 (no description = no penalty)
            
            # Path shape score
            path_score = self.compute_path_shape_score(
                query_meta['skeleton_path'],
                meta['skeleton_path']
            )
            
            # Keyword overlap
            keyword_score = self.compute_keyword_overlap_score(
                query_meta['keywords'],
                meta['keywords']
            )
            
            # Category match with flexible matching for "ATTRIBUTE, CONSTRAINT"
            category_score = 0.0
            query_cat = query_meta['category']
            doc_cat = meta['category']
            
            if query_cat == doc_cat:
                # Exact match
                category_score = 1.0
            elif query_cat == 'ATTRIBUTE, CONSTRAINT':
                # Query is extension value - match ATTRIBUTE, CONSTRAINT, or "ATTRIBUTE, CONSTRAINT"
                # This is a PERFECT match since the query means "could be either ATTRIBUTE or CONSTRAINT"
                if doc_cat in ['ATTRIBUTE', 'CONSTRAINT', 'ATTRIBUTE, CONSTRAINT']:
                    category_score = 1.0  # Perfect match - extension can be either category
                else:
                    category_score = 0.0
            elif doc_cat == 'ATTRIBUTE, CONSTRAINT':
                # Doc is extension value - match ATTRIBUTE or CONSTRAINT queries
                # This is also a perfect match since doc means "could be either"
                if query_cat in ['ATTRIBUTE', 'CONSTRAINT']:
                    category_score = 1.0  # Perfect match - extension matches specific category
                else:
                    category_score = 0.0
            else:
                # No match
                category_score = 0.0
            
            # Neighbor keyword boost
            # Boosts results if their parent/child nodes have keywords that match the query
            # This helps find statements in the right structural context
            # Example: "pattern" query gets boost if parent is "type" (since patterns appear in types)
            neighbor_boost = 0.0
            node_id = meta['node_id']
            if node_id in neighbor_info:
                neighbor_keywords = set()
                for n in neighbor_info[node_id]:
                    neighbor_keywords.update(n.get('keywords', []))
                
                if neighbor_keywords and query_meta['keywords']:
                    # Calculate overlap between query keywords and neighbor keywords
                    query_kw_set = set(query_meta['keywords'])
                    overlap = len(query_kw_set & neighbor_keywords)
                    neighbor_boost = overlap / len(query_kw_set)
                    
                    # Alternative: also consider if neighbors have common YANG structural keywords
                    # This gives boost even if exact keywords don't match
                    # E.g., "regexp-posix" in a leaf/type context gets boost for type-related neighbors
                    structural_neighbors = neighbor_keywords & self.STRUCTURAL_KEYWORDS
                    if structural_neighbors and not overlap:
                        # Small boost (0.1) for being in right structural context
                        neighbor_boost = 0.1
            
            # Token matching - compare masked tokens between query and document
            # Matching value types (STRING, LIST, REGEX, NO_VALUE) is crucial for YANG
            all_tokens = ['<STRING>', '<IDENTIFIER>', '<TYPEDEF>', '<REGEX>', '<BOOLEAN>',
                         '<NUMBER>', '<VERSION>', '<RANGE>', '<IP_ADDRESS>', '<MAC_ADDRESS>', '<LIST>', '<NO_VALUE>']

            query_snippet = query_meta.get('snippet_masked', '')
            doc_snippet = meta.get('snippet_masked', '')

            def normalize_tokens(snippet):
                """Extract tokens, treating <IDENTIFIER> and <STRING> as equivalent (<STRING>)."""
                tokens = set()
                for t in all_tokens:
                    if t in snippet:
                        # Normalize: <IDENTIFIER> and <STRING> share the same weight
                        tokens.add('<STRING>' if t == '<IDENTIFIER>' else t)
                return tokens

            # Extract tokens from both snippets (normalized)
            query_tokens = normalize_tokens(query_snippet)
            doc_tokens = normalize_tokens(doc_snippet)

            if query_tokens and doc_tokens:
                # Both have tokens: compute overlap score using normalized tokens
                matching_tokens = query_tokens & doc_tokens
                token_score = len(matching_tokens) / len(query_tokens)
            elif query_tokens and not doc_tokens:
                # Query expects a typed value but doc has none (raw/unmasked text) — penalize
                token_score = 0.0
            else:
                # Query has no tokens (flag/structural) or neither has tokens — no penalty
                token_score = 1.0
            
            # Compute lexical overlap between query text and doc's raw snippet
            qtext_for_lex = query_meta.get('raw_query', '') or query_meta.get('snippet_masked', '') or ''
            # Use display_text stored in metadata as document-side raw snippet proxy
            dtext_for_lex = meta.get('display_text', '') or meta.get('snippet_masked', '') or ''
            lexical_overlap = self._lexical_overlap_score(qtext_for_lex, dtext_for_lex)

            # BM25 normalized
            bm25_norm = 0.0
            if bm25_scores_all is not None and bm25_max > 0:
                bm25_norm = float(bm25_scores_all[idx] / bm25_max)
            
            # Calculate final score using normalized weights (sum = 1.0)
            # Each component score is in [0, 1], so final_score is naturally in [0, 1]
            final_score = (
                self.base_weights['structure_sim'] * structure_sim +
                self.base_weights['description_sim'] * description_sim +
                self.base_weights['path_shape'] * path_score +
                self.base_weights['keyword_overlap'] * keyword_score +
                self.base_weights['category_match'] * category_score +
                self.base_weights['neighbor_boost'] * neighbor_boost +
                self.base_weights['lexical_overlap'] * lexical_overlap +
                self.base_weights.get('bm25', 0.0) * bm25_norm +
                self.base_weights['token_match'] * token_score
            )
            
            score_components = {
                'structure_sim': float(structure_sim),
                'description_sim': float(description_sim),
                'path_shape': path_score,
                'keyword_overlap': keyword_score,
                'category_match': category_score,
                'neighbor_boost': neighbor_boost,
                'lexical_overlap': lexical_overlap,
                'bm25': bm25_norm,
                'token_score': token_score,
                'final_score': final_score,
                # Expose the fixed weights used for scoring
                'weights': {
                    'structure_sim': self.base_weights.get('structure_sim', 0.0),
                    'description_sim': self.base_weights.get('description_sim', 0.0),
                    'path_shape': self.base_weights.get('path_shape', 0.0),
                    'keyword_overlap': self.base_weights.get('keyword_overlap', 0.0),
                    'category_match': self.base_weights.get('category_match', 0.0),
                    'neighbor_boost': self.base_weights.get('neighbor_boost', 0.0),
                    'lexical_overlap': self.base_weights.get('lexical_overlap', 0.0),
                    'bm25': self.base_weights.get('bm25', 0.0),
                    'token_match': self.base_weights.get('token_match', 0.0)
                }
            }
            
            results.append((idx, final_score, score_components))
        
        # Sort by final score, with index as tiebreaker after rounding to 5 decimal
        # places. Scores that differ only at the 6th+ decimal place are BLAS noise
        # (float32 dot-product results differ between Windows and Linux BLAS backends)
        # and must not affect the ranking — the document index provides a stable,
        # platform-independent tiebreaker.
        results.sort(key=lambda x: (-round(x[1], 5), x[0]))

        return results
    
    def _build_alternate_query(self, query: str, from_token: str, to_token: str) -> Optional[str]:
        """Build an alternate query substituting ``from_token`` with ``to_token``.

        Handles TOKEN_EQUIVALENTS pairs:
          <STRING>  ↔ <IDENTIFIER>: quoted "lower" ↔ unquoted lower
          <RANGE>   ↔ <LENGTH>:     same value, skip (no transformation needed)

        Returns None if the transformation is not safe/applicable.
        """
        q = query.strip()

        # Detect structural block query: ends with "{};" (with optional whitespace)
        is_structural = bool(re.search(r'\{\}\s*;\s*$', q))

        if is_structural:
            base = re.sub(r'\s*\{\}\s*;\s*$', '', q).strip()
            suffix = ' {};'
        else:
            base = q.rstrip(';').strip()
            suffix = ';'

        parts = base.split(None, 1)
        if len(parts) < 2:
            return None
        kw, raw_val = parts[0], parts[1].strip()

        if from_token == '<STRING>' and to_token == '<IDENTIFIER>':
            # Strip quotes from a simple quoted identifier
            if ((raw_val.startswith('"') and raw_val.endswith('"')) or
                    (raw_val.startswith("'") and raw_val.endswith("'"))):
                val = raw_val[1:-1]
                if self._SIMPLE_IDENTIFIER_RE.match(val):
                    return f'{kw} {val}{suffix}'

        elif from_token == '<IDENTIFIER>' and to_token == '<STRING>':
            # Add quotes around an unquoted simple identifier
            if self._SIMPLE_IDENTIFIER_RE.match(raw_val):
                return f'{kw} "{raw_val}"{suffix}'

        elif from_token in ('<RANGE>', '<LENGTH>') and to_token in ('<RANGE>', '<LENGTH>'):
            # Same value — skip to avoid duplicate query
            return None

        return None

    def _make_display_template(self, primary_template: str,
                               active_token_pairs: List[tuple]) -> str:
        """Build a display template showing all token variants used in alternate queries.

        For each (from_tok, to_tok) pair that was actually used to build an alternate
        query, replaces ``from_tok`` in the primary template with ``from_tok|to_tok``.

        Example:
            primary_template = 'smiv2:max-access <STRING> ;'
            active_token_pairs = [('<STRING>', '<IDENTIFIER>')]
            → 'smiv2:max-access <STRING>|<IDENTIFIER> ;'
        """
        display = primary_template
        for from_tok, to_tok in active_token_pairs:
            if from_tok in display:
                display = display.replace(from_tok, f'{from_tok}|{to_tok}', 1)
        return display

    def query(self, query_text: str,
              top_k: int = 10,
              initial_k: int = 20,
              show_scores: bool = False,
              category_filter: Optional[str] = None,
              yang_file: Optional[str] = None,
              max_keyword_occurrences: int = 100) -> List[Dict[str, Any]]:
        """
        Main query method.

        Runs the primary query, then for each TOKEN_EQUIVALENTS pair checks if
        the primary template contains one of the tokens and runs an alternate query
        with the equivalent token substituted. Results from all queries are merged,
        keeping the best score per index position, then re-sorted and top-K returned.
        
        Args:
            query_text: Raw YANG snippet
            top_k: Final top-K to return
            initial_k: Initial retrieval size before reranking
            show_scores: Show score breakdown
            category_filter: Filter by category
            yang_file: Optional path to full YANG file (for accurate path extraction)
            max_keyword_occurrences: Maximum keyword matches to add (default 200)
        
        Returns:
            List of result dicts with metadata and scores
        """
        # Build query template (with optional YANG file for accurate path extraction)
        query_template, query_meta = self.build_query_template(query_text, yang_file)

        # Determine which alternate queries to run based on TOKEN_EQUIVALENTS.
        # Track both the alternate query strings and the (from_tok, to_tok) pairs
        # that were actually used — the pairs are needed for the display template.
        alt_queries: List[str] = []           # alternate raw query strings
        active_token_pairs: List[tuple] = []  # (from_tok, to_tok) pairs actually used
        queries_run: set = {query_text}
        for token_a, token_b in self.TOKEN_EQUIVALENTS:
            for from_tok, to_tok in [(token_a, token_b), (token_b, token_a)]:
                if from_tok in query_template:
                    alt_q = self._build_alternate_query(query_text, from_tok, to_tok)
                    if alt_q and alt_q not in queries_run:
                        queries_run.add(alt_q)
                        alt_queries.append(alt_q)
                        active_token_pairs.append((from_tok, to_tok))

        # Build display template showing all token variants (e.g. <STRING>|<IDENTIFIER>)
        display_template = self._make_display_template(query_template, active_token_pairs)

        console.print(f"\n[bold cyan]Query Analysis:[/bold cyan]")
        console.print(f"  Category: {query_meta['category']}")
        console.print(f"  Skeleton Path: {query_meta['skeleton_path']}")
        console.print(f"  Keywords: {', '.join(query_meta['keywords']) if query_meta['keywords'] else 'None'}")
        console.print(f"  Template: {display_template}")
        if alt_queries:
            console.print(f"  [dim]Alternate queries: {len(alt_queries)} (token equivalents)[/dim]")
        
        # Pre-filter - use detected category for flexible matching
        # If user provides explicit category_filter, use that; otherwise use detected category
        filter_category = category_filter if category_filter is not None else query_meta['category']
        prefilter_mask = self.prefilter(query_meta, category_filter=filter_category)
        valid_count = np.sum(prefilter_mask)
        console.print(f"\n[dim]Pre-filter: {valid_count}/{len(self.metadata)} candidates[/dim]")
        
        # For "ATTRIBUTE, CONSTRAINT" queries, increase initial_k to capture more candidates
        # These are extension values that need to match both attributes and constraints
        # Token matching happens in reranking, so we need a MUCH wider initial pool
        # This is crucial: privileges <LIST> needs to find key <LIST> in initial candidates
        if query_meta['category'] == 'ATTRIBUTE, CONSTRAINT':
            initial_k = max(initial_k, _RAG_INITIAL_K)  # Wide pool: standard YANG equivalents may be at rank ~2500
        # For ATTRIBUTE queries with known keywords, also increase pool to overcome semantic drift
        # Example: "path" and "prefix" have high semantic similarity, need wider search
        elif query_meta['category'] == 'ATTRIBUTE' and query_meta['keywords']:
            initial_k = max(initial_k, _RAG_INITIAL_K)  # Wide pool: standard YANG equivalents may be at rank ~2500
        
        # Vector search
        candidate_indices = self.vector_search(query_template, prefilter_mask, top_k=initial_k)
        console.print(f"[dim]Vector search: {len(candidate_indices)} initial results[/dim]")
        
        if not candidate_indices:
            console.print("[yellow]No results found[/yellow]")
            return []
        
        # For known YANG keywords, ensure keyword-matching documents are included
        # This prevents semantic drift (e.g., "path" matching only "prefix")
        if query_meta['keywords'] and query_meta['keywords'][0] in self.ALL_KEYWORDS:
            console.print(f"[dim]Keyword matching: checking up to {max_keyword_occurrences} candidates for '{query_meta['keywords'][0]}'[/dim]")
            # Check if any candidates have the exact keyword
            has_keyword_match = False
            for idx in candidate_indices[:max_keyword_occurrences]:  # Check first N based on parameter
                if query_meta['keywords'][0] in self.metadata[idx].get('keywords', []):
                    has_keyword_match = True
                    break
            
            # If no keyword matches in top results, add some via keyword search
            if not has_keyword_match and prefilter_mask is not None:
                keyword_to_find = query_meta['keywords'][0]
                keyword_candidates = []
                valid_indices = np.where(prefilter_mask)[0]
                
                for idx in valid_indices:
                    if keyword_to_find in self.metadata[idx].get('keywords', []):
                        keyword_candidates.append(idx)
                        if len(keyword_candidates) >= max_keyword_occurrences:
                            break
                
                if keyword_candidates:
                    # Replace bottom N vector results with keyword matches (N = min of half initial_k or occurrence limit)
                    replace_count = min(len(candidate_indices) // 2, max_keyword_occurrences)
                    candidate_indices = candidate_indices[:-replace_count] + keyword_candidates
                    console.print(f"[dim]Added {len(keyword_candidates)} keyword-matched candidates[/dim]")

        # Keyword coverage: guarantee every standard YANG keyword that has
        # pre-filtered matches is represented by at least one doc in the pool.
        #
        # Why: sentence-transformers uses PyTorch, whose CPU backend differs
        # between platforms (MKL on Windows, OpenBLAS on Linux). This shifts
        # ALL similarity scores slightly, causing low-frequency keywords whose
        # only docs land near the initial-k boundary to fall in/out of the pool
        # depending on the platform. This pass is purely deterministic (no
        # floats — just set membership and sorted iteration over fixed indices)
        # so it produces the same candidate pool on every platform.
        _cand_set = set(candidate_indices)
        _valid_idxs = np.where(prefilter_mask)[0]   # already sorted ascending
        _K_COV = 5                                   # max reps per keyword
        _kw_reps: Dict[str, List[int]] = {}
        for _vi in _valid_idxs:
            _vi = int(_vi)
            for _kw in self.metadata[_vi].get('keywords', []):
                if _kw in self.ALL_KEYWORDS:
                    lst = _kw_reps.setdefault(_kw, [])
                    if len(lst) < _K_COV:
                        lst.append(_vi)
        _added = 0
        for _kw in sorted(_kw_reps):               # sorted → deterministic order
            if not any(i in _cand_set for i in _kw_reps[_kw]):
                for _vi in _kw_reps[_kw]:
                    candidate_indices.append(_vi)
                    _cand_set.add(_vi)
                    _added += 1
        if _added:
            console.print(f"[dim]Keyword coverage: +{_added} docs for underrepresented YANG keywords[/dim]")

        # Rerank primary query (compute both structure and description query vectors)
        query_structure_vec = self.model.encode(query_template, show_progress=False)
        # For description, use the query text directly (not the masked template)
        query_description_vec = self.model.encode(query_text, show_progress=False) if self.description_embeddings is not None else None
        ranked = self.rerank(candidate_indices, query_meta, query_structure_vec, query_description_vec)

        # Run alternate queries (TOKEN_EQUIVALENTS) and merge results.
        # For each alternate query, run the full pipeline and merge keeping best score per index.
        if alt_queries:
            # Build a dict: index → (score, components) from primary results
            merged: Dict[int, Tuple[int, float, Dict]] = {idx: (idx, score, comp) for idx, score, comp in ranked}

            for alt_q in alt_queries:
                console.print(f"[dim]Running alternate query: {self.mask_snippet(alt_q)}[/dim]")
                alt_template, alt_meta = self.build_query_template(alt_q, yang_file)
                alt_prefilter = self.prefilter(alt_meta, category_filter=filter_category)
                alt_initial_k = initial_k
                if alt_meta['category'] in ('ATTRIBUTE, CONSTRAINT', 'ATTRIBUTE'):
                    alt_initial_k = max(alt_initial_k, _RAG_INITIAL_K)
                alt_candidates = self.vector_search(alt_template, alt_prefilter, top_k=alt_initial_k)
                if alt_candidates:
                    alt_struct_vec = self.model.encode(alt_template, show_progress=False)
                    alt_desc_vec = self.model.encode(alt_q, show_progress=False) if self.description_embeddings is not None else None
                    alt_ranked = self.rerank(alt_candidates, alt_meta, alt_struct_vec, alt_desc_vec)
                    for idx, score, comp in alt_ranked:
                        if idx not in merged or score > merged[idx][1]:
                            merged[idx] = (idx, score, comp)
                    console.print(f"[dim]  → {len(alt_ranked)} results merged[/dim]")

            # Re-sort merged results — same stable rounding tiebreaker as rerank()
            ranked = sorted(merged.values(), key=lambda x: (-round(x[1], 5), x[0]))

        # Format results
        results = []
        for idx, score, components in ranked[:top_k]:
            meta = self.metadata[idx]
            result = {
                'rank': len(results) + 1,
                'node_id': meta['node_id'],
                'category': meta['category'],
                'skeleton_path': meta['skeleton_path'],
                'keywords': meta['keywords'],
                'snippet': meta['snippet_masked'],
                'snippet_masked': meta['snippet_masked'],
                'module': meta['module'],
                'path': meta['path'],
                'yang_keyword': meta['yang_keyword'],
                'display_text': meta['display_text'],
                'score': score,
                'score_components': components if show_scores else None
            }
            results.append(result)

        # Append synthetic entries for metadata/flag attributes that have no indexed corpus documents.
        # - 'name' (<STRING>): node identifier attribute — not a YANG statement, never indexed
        # - flag-type keywords (<NO_VALUE>): presence/absence flags (mandatory-flag, hint-flag, etc.)
        #   built dynamically from KEYWORD_XML_TYPES so no hardcoding is needed.
        # Injected only when the query category includes ATTRIBUTE (not STRUCTURAL)
        # and the query template contains the matching mask token.
        # Score is computed using the same WEIGHTS as rerank() for fair comparison.
        query_cat = query_meta.get('category', '')
        is_attribute_query = query_cat in ('ATTRIBUTE', 'ATTRIBUTE, CONSTRAINT')
        if is_attribute_query:
            query_tmpl_tokens = set(re.findall(r'<[A-Z_]+>', query_template))
            synthetic_defs = []
            if '<STRING>' in query_tmpl_tokens:
                synthetic_defs.append(('name', 'name <STRING> ;'))
            # 'path' is already indexed in the corpus — no synthetic entry needed
            # Inject flag-type keywords when query has <NO_VALUE> mask
            if '<NO_VALUE>' in query_tmpl_tokens:
                for kw, xml_type in _KEYWORD_XML_TYPES.items():
                    if xml_type == 'flag':
                        synthetic_defs.append((kw, f'{kw} <NO_VALUE> ;'))

            for syn_kw, syn_snippet in synthetic_defs:
                # Compute score components using the same weights as rerank()
                syn_vec = self.model.encode(syn_snippet, show_progress=False)
                structure_sim = float(compute_similarity(query_structure_vec, syn_vec.reshape(1, -1), metric='cosine')[0])
                description_sim = 1.0  # no description = no penalty
                path_score = self.compute_path_shape_score(query_meta['skeleton_path'], 'module')
                keyword_score = self.compute_keyword_overlap_score(query_meta['keywords'], [syn_kw])
                category_score = 1.0  # always ATTRIBUTE, matching query
                neighbor_boost = 0.0  # no neighbors for synthetic entries
                lexical_overlap = self._lexical_overlap_score(
                    query_meta.get('raw_query', '') or query_meta.get('snippet_masked', '') or '',
                    syn_snippet
                )
                bm25_norm = 0.0  # not in BM25 corpus
                # Token match: compare mask tokens
                all_tokens = ['<STRING>', '<IDENTIFIER>', '<TYPEDEF>', '<REGEX>', '<BOOLEAN>',
                              '<NUMBER>', '<VERSION>', '<RANGE>', '<IP_ADDRESS>', '<MAC_ADDRESS>', '<LIST>', '<NO_VALUE>']
                def _norm(snippet):
                    """Normalize <IDENTIFIER> and <STRING> to the same token."""
                    tokens = set()
                    for t in all_tokens:
                        if t in snippet:
                            tokens.add('<STRING>' if t == '<IDENTIFIER>' else t)
                    return tokens
                query_tokens = _norm(query_template)
                syn_tokens = _norm(syn_snippet)
                if query_tokens and syn_tokens:
                    token_score = len(query_tokens & syn_tokens) / len(query_tokens)
                else:
                    token_score = 1.0

                final_score = (
                    self.base_weights['structure_sim'] * structure_sim +
                    self.base_weights['description_sim'] * description_sim +
                    self.base_weights['path_shape'] * path_score +
                    self.base_weights['keyword_overlap'] * keyword_score +
                    self.base_weights['category_match'] * category_score +
                    self.base_weights['neighbor_boost'] * neighbor_boost +
                    self.base_weights['lexical_overlap'] * lexical_overlap +
                    self.base_weights.get('bm25', 0.0) * bm25_norm +
                    self.base_weights['token_match'] * token_score
                )
                score_components = {
                    'structure_sim': structure_sim,
                    'description_sim': description_sim,
                    'path_shape': path_score,
                    'keyword_overlap': keyword_score,
                    'category_match': category_score,
                    'neighbor_boost': neighbor_boost,
                    'lexical_overlap': lexical_overlap,
                    'bm25': bm25_norm,
                    'token_score': token_score,
                    'final_score': final_score,
                    'weights': {k: self.base_weights.get(k, 0.0) for k in
                                ['structure_sim', 'description_sim', 'path_shape', 'keyword_overlap',
                                 'category_match', 'neighbor_boost', 'lexical_overlap', 'bm25', 'token_match']},
                }
                results.append({
                    'rank': len(results) + 1,
                    'node_id': f'synthetic:{syn_kw}',
                    'category': 'ATTRIBUTE',
                    'skeleton_path': 'module',
                    'keywords': [syn_kw],
                    'snippet': syn_snippet,
                    'snippet_masked': syn_snippet,
                    'module': '(built-in)',
                    'path': '',
                    'yang_keyword': syn_kw,
                    'display_text': syn_snippet,
                    'score': final_score,
                    'score_components': score_components if show_scores else None,
                })
                console.print(f"[dim]Appended synthetic '{syn_kw}' entry (ATTRIBUTE, score={final_score:.4f})[/dim]")

        return results
    
    def print_results(self, results: List[Dict[str, Any]], show_scores: bool = False):
        """Pretty print results."""
        if not results:
            console.print("\n[yellow]No results found[/yellow]")
            return
        
        console.print(f"\n[bold green]Top {len(results)} Results:[/bold green]\n")
        
        for r in results:
            # Header
            console.print(f"[bold cyan]#{r['rank']} | Score: {r['score']:.4f} | {r['category']}[/bold cyan]")
            
            # Table
            table = Table(show_header=False, box=None, padding=(0, 1))
            table.add_column("Key", style="dim")
            table.add_column("Value")
            
            table.add_row("Module", r['module'])
            table.add_row("Skeleton Path", r['skeleton_path'])
            table.add_row("Keywords", ', '.join(r['keywords']) if r['keywords'] else 'None')
            table.add_row("YANG Keyword", r['yang_keyword'])
            table.add_row("Snippet", r['snippet'][:100])
            
            console.print(table)
            
            # Score components
            if show_scores and r['score_components']:
                comp = r['score_components']
                console.print(
                    f"[dim]  Scores: struct={comp['structure_sim']:.3f} | desc={comp['description_sim']:.3f} | "
                    f"path={comp['path_shape']:.3f} | kw={comp['keyword_overlap']:.3f} | "
                    f"cat={comp['category_match']:.3f} | nbr={comp['neighbor_boost']:.3f} | "
                    f"lex={comp.get('lexical_overlap', 0.0):.3f} | tok={comp.get('token_score', 0.0):.3f}[/dim]"
                )
                w = comp.get('weights') or {}
                console.print(
                    f"[dim]  Weights: struct={w.get('structure_sim',0.0):.2f} | desc={w.get('description_sim',0.0):.2f} | "
                    f"path={w.get('path_shape',0.0):.2f} | kw={w.get('keyword_overlap',0.0):.2f} | "
                    f"cat={w.get('category_match',0.0):.2f} | nbr={w.get('neighbor_boost',0.0):.2f} | "
                    f"lex={w.get('lexical_overlap',0.0):.2f} | bm25={w.get('bm25',0.0):.2f} | token={w.get('token_match',0.0):.2f}[/dim]"
                )
            
            console.print()

    def print_results_with_context(self, results: List[Dict[str, Any]], show_scores: bool = False, max_children: int = 2):
        """Pretty print results with parent/children context from graph/metadata."""
        if not results:
            console.print("\n[yellow]No results found[/yellow]")
            return
        console.print(f"\n[bold green]Top {len(results)} Results (with context):[/bold green]\n")
        for r in results:
            console.print(f"[bold cyan]#{r['rank']} | Score: {r['score']:.4f} | {r['category']}[/bold cyan]")
            table = Table(show_header=False, box=None, padding=(0, 1))
            table.add_column("Key", style="dim")
            table.add_column("Value")
            table.add_row("Module", r['module'])
            table.add_row("Skeleton Path", r['skeleton_path'])
            table.add_row("Keywords", ', '.join(r['keywords']) if r['keywords'] else 'None')
            table.add_row("YANG Keyword", r['yang_keyword'])
            table.add_row("Snippet", r['snippet'][:100])
            console.print(table)

            nid = r['node_id']
            ninfo = self.expand_neighbors([nid]).get(nid, [])
            parent = next((x for x in ninfo if x.get('type') == 'parent'), None)
            children = [x for x in ninfo if x.get('type') == 'child'][:max_children]

            def _meta_line(node_id: str) -> str:
                idx = self.node_index_map.get(node_id)
                if idx is None:
                    return "<unknown>"
                m = self.metadata[idx]
                return f"{m.get('yang_keyword','')} :: {m.get('display_text','')[:140]}"

            if parent:
                console.print(f"  [dim]Parent:[/dim] {_meta_line(parent['id'])}")
            for i, c in enumerate(children, 1):
                console.print(f"  [dim]Child {i}:[/dim] {_meta_line(c['id'])}")

            if show_scores and r['score_components']:
                comp = r['score_components']
                console.print(
                    f"[dim]  Scores: struct={comp['structure_sim']:.3f} | desc={comp['description_sim']:.3f} | "
                    f"path={comp['path_shape']:.3f} | kw={comp['keyword_overlap']:.3f} | "
                    f"cat={comp['category_match']:.3f} | nbr={comp['neighbor_boost']:.3f} | "
                    f"lex={comp.get('lexical_overlap', 0.0):.3f} | tok={comp.get('token_score', 0.0):.3f}[/dim]"
                )
                w = comp.get('weights') or {}
                console.print(
                    f"[dim]  Weights: struct={w.get('structure_sim',0.0):.2f} | desc={w.get('description_sim',0.0):.2f} | "
                    f"path={w.get('path_shape',0.0):.2f} | kw={w.get('keyword_overlap',0.0):.2f} | "
                    f"cat={w.get('category_match',0.0):.2f} | nbr={w.get('neighbor_boost',0.0):.2f} | "
                    f"lex={w.get('lexical_overlap',0.0):.2f} | bm25={w.get('bm25',0.0):.2f} | token={w.get('token_match',0.0):.2f}[/dim]"
                )
            console.print()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Query YANG-RAG index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python rag/query.py "leaf interface-name { type string; }"
  python rag/query.py "pattern '[0-9]+'" --category CONSTRAINT
  python rag/query.py "must ..." --top-k 5 --show-scores
        """
    )
    parser.add_argument(
        'query',
        type=str,
        help='YANG snippet to search for'
    )
    parser.add_argument(
        '--index-dir',
        type=Path,
        default=INDEX_DIR,
        help='Index directory'
    )
    parser.add_argument(
        '--top-k',
        type=int,
        default=10,
        help='Number of results to return'
    )
    parser.add_argument(
        '--initial-k',
        type=int,
        default=20,
        help='Initial retrieval size before reranking'
    )
    parser.add_argument(
        '--category',
        type=str,
        choices=['STRUCTURAL', 'CONSTRAINT', 'ATTRIBUTE', 'EXTENSION', 'UNKNOWN'],
        help='Filter by category'
    )
    parser.add_argument(
        '--show-scores',
        action='store_true',
        help='Show score breakdown'
    )
    parser.add_argument(
        '--show-context',
        action='store_true',
        help='Print parent and children context for each result'
    )
    parser.add_argument(
        '--max-keyword-occurrences',
        type=int,
        default=100,
        help='Maximum keyword matches to include (default: 100)'
    )
    parser.add_argument(
        '--weights',
        type=str,
        help='Override base weights as comma-separated key=value (e.g., vector_sim=0.40,path_shape=0.25,lexical_overlap=0.05)'
    )
    
    args = parser.parse_args()
    
    # Query
    console.print("[bold cyan]YANG-RAG Query Engine[/bold cyan]")
    
    # Parse weight overrides if provided
    weights_override = None
    if args.weights:
        weights_override = {}
        for pair in args.weights.split(','):
            if '=' in pair:
                k, v = pair.split('=', 1)
                weights_override[k.strip()] = float(v)

    engine = YANGQuery(index_dir=args.index_dir, weights_override=weights_override)
    results = engine.query(
        args.query,
        top_k=args.top_k,
        initial_k=args.initial_k,
        show_scores=args.show_scores,
        category_filter=args.category,
        max_keyword_occurrences=args.max_keyword_occurrences
    )
    
    if args.show_context:
        engine.print_results_with_context(results, show_scores=args.show_scores)
    else:
        engine.print_results(results, show_scores=args.show_scores)


if __name__ == '__main__':
    main()
