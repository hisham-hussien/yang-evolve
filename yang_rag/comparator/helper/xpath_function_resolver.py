#!/usr/bin/env python3
"""
xpath_function_resolver.py - RFC 7950 Section 10 XPath built-in function resolver

Augments the base XPathResolver with support for YANG built-in XPath functions:

Phase 1 (high value):
  - current()          : RFC 7950 §10.6.1 — context node reference
  - not(expr)          : XPath 1.0 — boolean negation wrapper
  - Predicate stripping: list[key=val]/child -> list/child structural path

Phase 2 (medium value):
  - derived-from(path, identity)          : RFC 7950 §10.4.1
  - derived-from-or-self(path, identity)  : RFC 7950 §10.4.2
  - count(node-set)    : XPath 1.0
  - boolean(expr)      : XPath 1.0
  - number(expr)       : XPath 1.0

Phase 3 (limited path relevance):
  - enum-value(node-set)       : RFC 7950 §10.6.1
  - bit-is-set(node-set, bit)  : RFC 7950 §10.6.2
  - deref(node-set)            : RFC 7950 §10.3.1
  - re-match(string, pattern)  : RFC 7950 §10.2.1 (no path — returns None)
  - string-length, contains, normalize-space, substring, concat (no path)

Real patterns from OpenConfig YANG files:
  current()/../../../../config/name
  acl-set[name=current()/../../../../set-name][type=current()/../../../../type]/seq-id
  when "current()/oc-if:config/oc-if:type = 'ianaift:l3ipvlan'"
  must "../../operational-modes[mode-id=current()]/mode-id"
  when "../config[contains(services, 'oc-gnsi:GNSI')]/enable = 'true'"
"""

import re
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FunctionParseResult:
    """Result of parsing an XPath expression that may contain built-in functions."""
    
    # The primary structural path (predicates stripped, functions resolved)
    # e.g. "../../acl-set/acl-entries/acl-entry/sequence-id"
    primary_path: str
    
    # Whether the expression contained any built-in functions
    had_functions: bool = False
    
    # Which functions were detected
    functions_found: List[str] = field(default_factory=list)
    
    # Secondary paths found inside predicates (e.g. current()/../set-name)
    predicate_paths: List[str] = field(default_factory=list)
    
    # For derived-from / derived-from-or-self: the identity string argument
    identity_arg: Optional[str] = None
    
    # For bit-is-set: the bit name argument
    bit_name_arg: Optional[str] = None
    
    # Whether the expression has no resolvable path (e.g. re-match on literals)
    no_path: bool = False
    
    # Human-readable explanation of what was done
    explanation: str = ""
    
    # Error if parsing failed
    error: Optional[str] = None


@dataclass
class ResolvedFunctionPath:
    """Full resolution result including function-aware processing."""
    
    # The resolved absolute schema path
    absolute_path: str
    
    # The context path where the XPath is attached
    context_path: str
    
    # The original raw XPath expression
    original_xpath: str
    
    # The parse result (what functions were found, etc.)
    parse_result: Optional[FunctionParseResult] = None
    
    # Whether resolution succeeded
    success: bool = True
    
    # Error message if failed
    error: Optional[str] = None
    
    # Secondary resolved paths (from predicates)
    secondary_paths: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core XPath function parser
# ---------------------------------------------------------------------------

class XPathFunctionParser:
    """
    Parses XPath expressions containing RFC 7950 and XPath 1.0 built-in
    functions, extracting resolvable schema paths for structural comparison.

    Design principle:
        For schema-path resolution we do NOT evaluate functions at runtime.
        Instead we extract the *path arguments* from each function so the
        caller can resolve them against the YANG schema tree.

    Supported functions (RFC 7950 §10 + XPath 1.0 wrappers):
        Phase 1 — current(), not(), predicate stripping
        Phase 2 — derived-from(), derived-from-or-self(), count(),
                   boolean(), number()
        Phase 3 — enum-value(), bit-is-set(), deref(), re-match(),
                   string-length(), contains(), normalize-space(),
                   substring(), concat()
    """

    # Functions that take a node-set/expr as their SINGLE argument
    # (yangson: UnaryExpr subclasses with one path/expr arg)
    # Confirmed from yangson xpathast.py:
    #   FuncNot, FuncCount, FuncBoolean, FuncNumber, FuncEnumValue,
    #   FuncStringLength, FuncNormalizeSpace, FuncSum, FuncCeiling,
    #   FuncFloor, FuncRound, FuncDeref (handled separately)
    SINGLE_PATH_FUNCTIONS = {
        'not', 'count', 'boolean', 'number',
        'enum-value', 'sum', 'ceiling', 'floor', 'round',
    }

    # Functions with OPTIONAL single node-set arg (no arg = use context node)
    # (yangson: UnaryExpr with _opt_arg())
    # FuncStringLength, FuncNormalizeSpace, FuncString, FuncName
    OPTIONAL_PATH_FUNCTIONS = {
        'string-length', 'normalize-space', 'string',
        'local-name', 'name',
    }

    # Functions that take (node-set, string-literal) — path is first arg
    # (yangson: BinaryExpr subclasses with _two_args())
    # FuncDerivedFrom, FuncBitIsSet
    PATH_THEN_LITERAL_FUNCTIONS = {
        'derived-from', 'derived-from-or-self', 'bit-is-set',
    }

    # Functions that take (string, string[, string]) — NO resolvable path
    # (yangson: BinaryExpr/variadic with string args)
    # FuncReMatch, FuncContains, FuncStartsWith, FuncConcat,
    # FuncSubstring, FuncSubstringBefore, FuncSubstringAfter, FuncTranslate
    NO_PATH_FUNCTIONS = {
        're-match', 'contains', 'substring', 'concat',
        'starts-with', 're-match', 'translate',
        'substring-before', 'substring-after',
    }

    # Positional / context — no path, no meaningful arg
    # (yangson: FuncLast, FuncPosition, FuncTrue, FuncFalse)
    POSITIONAL_FUNCTIONS = {'last', 'position', 'true', 'false'}

    def __init__(self):
        # Compiled regex patterns
        self._re_current = re.compile(r'\bcurrent\(\)')
        self._re_func = re.compile(
            r'\b([a-zA-Z][a-zA-Z0-9_-]*)\s*\('
        )
        self._re_prefix_strip = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]*:')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, xpath_expr: str, context_path: str) -> FunctionParseResult:
        """
        Parse an XPath expression, resolving built-in functions to their
        path arguments.

        Args:
            xpath_expr:   Raw XPath string from a when/must/leafref statement
            context_path: Absolute schema path of the node where the XPath
                          is attached (e.g. /module/container/leaf)

        Returns:
            FunctionParseResult with primary_path and metadata
        """
        expr = xpath_expr.strip()

        # Strip outer quotes if present
        if len(expr) >= 2 and expr[0] in ('"', "'") and expr[-1] == expr[0]:
            expr = expr[1:-1].strip()

        functions_found: List[str] = []
        predicate_paths: List[str] = []
        identity_arg: Optional[str] = None
        bit_name_arg: Optional[str] = None
        had_functions = False
        explanation_parts: List[str] = []

        # ---- Step 1: detect which functions are present ----
        for m in self._re_func.finditer(expr):
            fname = m.group(1)
            if fname not in ('current',) and fname not in self.POSITIONAL_FUNCTIONS:
                functions_found.append(fname)
                had_functions = True
        if self._re_current.search(expr):
            had_functions = True
            if 'current' not in functions_found:
                functions_found.insert(0, 'current')

        # ---- Step 2: handle top-level function wrappers ----
        # Matches patterns like:
        #   func(args)              — function is the whole expression
        #   func(args) op value     — function result compared to value
        #   func(args)/path/cont    — function result navigated further (deref)
        # We find the function name and extract its balanced argument list.
        top_func_match = re.match(
            r'^([a-zA-Z][a-zA-Z0-9_-]*)\s*\(', expr
        )
        if top_func_match:
            fname = top_func_match.group(1)
            # Find the matching closing paren for this function call
            paren_start = expr.index('(', len(fname))
            inner, after = self._extract_balanced_parens(expr, paren_start)

            # after is everything after the closing ')' — could be:
            #   ""              -> func(args) alone
            #   " > 0"          -> func(args) > 0  (comparison)
            #   "/path/cont"    -> func(args)/path  (path continuation, e.g. deref)
            after = after.strip()

            if fname in self.NO_PATH_FUNCTIONS or fname in self.POSITIONAL_FUNCTIONS:
                # These functions operate on string/value arguments or have no
                # path-relevant arguments. No schema path to resolve.
                # Confirmed from yangson: FuncReMatch, FuncContains, FuncConcat,
                # FuncStartsWith, FuncSubstring*, FuncTranslate, FuncLast,
                # FuncPosition, FuncTrue, FuncFalse
                return FunctionParseResult(
                    primary_path='',
                    had_functions=True,
                    functions_found=[fname],
                    no_path=True,
                    explanation=(
                        f"Function '{fname}()' operates on string/value arguments "
                        f"or has no path-relevant arguments — no schema path to resolve"
                    )
                )

            if fname in self.SINGLE_PATH_FUNCTIONS:
                # Single node-set/expr argument — the inner content IS the path.
                # Confirmed from yangson: FuncNot, FuncCount, FuncBoolean,
                # FuncNumber, FuncEnumValue, FuncSum, FuncCeiling, FuncFloor,
                # FuncRound (all UnaryExpr with one mandatory arg)
                # 'after' is ignored (it's a comparison like "> 0")
                inner_result = self.parse(inner, context_path)
                inner_result.functions_found = [fname] + inner_result.functions_found
                inner_result.had_functions = True
                inner_result.explanation = (
                    f"Unwrapped {fname}() — inner path: {inner_result.primary_path}"
                )
                return inner_result

            if fname in self.OPTIONAL_PATH_FUNCTIONS:
                # Optional node-set argument — if inner is empty, the function
                # uses the context node (no path to resolve). If inner is
                # non-empty, it's a path expression.
                # Confirmed from yangson: FuncStringLength, FuncNormalizeSpace,
                # FuncString, FuncName (all use _opt_arg())
                inner_stripped = inner.strip()
                if not inner_stripped:
                    # Called with no arg — uses context node, no path to resolve
                    return FunctionParseResult(
                        primary_path='.',
                        had_functions=True,
                        functions_found=[fname],
                        explanation=f"{fname}() called with no arg — uses context node"
                    )
                # Has an arg — parse it as a path
                inner_result = self.parse(inner_stripped, context_path)
                inner_result.functions_found = [fname] + inner_result.functions_found
                inner_result.had_functions = True
                inner_result.explanation = (
                    f"Unwrapped {fname}() — inner path: {inner_result.primary_path}"
                )
                return inner_result

            if fname in self.PATH_THEN_LITERAL_FUNCTIONS:
                # Two arguments: (node-set-path, string-literal)
                # Confirmed from yangson: FuncDerivedFrom, FuncBitIsSet
                # (_two_args() — first is path, second is literal)
                path_arg, lit_arg = self._split_first_arg(inner)
                inner_result = self.parse(path_arg.strip(), context_path)
                inner_result.functions_found = [fname] + inner_result.functions_found
                inner_result.had_functions = True
                lit_clean = lit_arg.strip().strip("'\"")
                if fname == 'bit-is-set':
                    inner_result.bit_name_arg = lit_clean
                else:
                    inner_result.identity_arg = lit_clean
                inner_result.explanation = (
                    f"Extracted path arg from {fname}(): {inner_result.primary_path}"
                    + (f", identity='{lit_clean}'" if fname != 'bit-is-set' else f", bit='{lit_clean}'")
                )
                return inner_result

            if fname == 'deref':
                # deref(node-set) — confirmed from yangson: FuncDeref(UnaryExpr)
                # Returns the node(s) that the leafref points to.
                # In yangson: PathExpr wraps deref() when followed by /path
                # e.g. deref(../iface)/config/type -> PathExpr(FuncDeref, loc_path)
                # For schema resolution: return the leafref SOURCE path (inner).
                # The continuation path 'after' requires runtime leafref traversal.
                inner_result = self.parse(inner, context_path)
                inner_result.functions_found = ['deref'] + inner_result.functions_found
                inner_result.had_functions = True
                inner_result.explanation = (
                    f"deref() — resolved leafref source path: {inner_result.primary_path}"
                    + (f", path continuation after deref: '{after}'" if after else "")
                    + " (deref target requires leafref path statement lookup at runtime)"
                )
                return inner_result

        # ---- Step 3: handle current() substitution ----
        # current() in XPath means "the initial context node" — for schema
        # path resolution it equals the context_path itself.
        working = expr
        if self._re_current.search(working):
            explanation_parts.append(f"current() substituted with context path '{context_path}'")
            # Replace current() with a sentinel that we can navigate from
            # We use __CONTEXT__ as a placeholder
            working = self._re_current.sub('__CONTEXT__', working)

        # ---- Step 4: extract the primary path (before operators) ----
        # The primary path is the leading path expression before any
        # comparison operator (=, !=, <, >, <=, >=) or boolean keyword
        primary_raw = self._extract_primary_path(working)

        # ---- Step 5: extract predicate paths and strip predicates ----
        pred_paths_raw = self._extract_predicate_paths(primary_raw)
        primary_stripped = self._strip_predicates(primary_raw)

        # ---- Step 6: resolve __CONTEXT__ placeholder ----
        primary_resolved = self._resolve_context_placeholder(
            primary_stripped, context_path
        )
        pred_paths_resolved = [
            self._resolve_context_placeholder(p, context_path)
            for p in pred_paths_raw
        ]

        # ---- Step 7: strip namespace prefixes ----
        primary_clean = self._strip_prefixes(primary_resolved)
        pred_paths_clean = [self._strip_prefixes(p) for p in pred_paths_resolved]

        if explanation_parts:
            explanation = '; '.join(explanation_parts)
        elif had_functions:
            explanation = f"Functions {functions_found} processed; primary path extracted"
        else:
            explanation = "No built-in functions detected; path extracted directly"

        return FunctionParseResult(
            primary_path=primary_clean,
            had_functions=had_functions,
            functions_found=functions_found,
            predicate_paths=pred_paths_clean,
            identity_arg=identity_arg,
            bit_name_arg=bit_name_arg,
            no_path=False,
            explanation=explanation
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_balanced_parens(self, expr: str, open_pos: int) -> Tuple[str, str]:
        """
        Given an expression and the position of an opening '(', find the
        matching closing ')' and return (inner_content, after_content).

        Example:
            expr = "count(../items) > 0", open_pos = 5
            returns ("../items", " > 0")

            expr = "deref(../iface)/config/type", open_pos = 5
            returns ("../iface", "/config/type")
        """
        depth = 0
        in_single = False
        in_double = False
        i = open_pos
        while i < len(expr):
            c = expr[i]
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        inner = expr[open_pos + 1:i]
                        after = expr[i + 1:]
                        return inner, after
            i += 1
        # Unbalanced — return everything after open as inner
        return expr[open_pos + 1:], ''

    def _extract_primary_path(self, expr: str) -> str:
        """
        Extract the leading path expression from an XPath expression,
        stopping at comparison operators or boolean keywords.

        Handles:
          "../../config/type = 'value'"  -> "../../config/type"
          "not(../foo)"                  -> handled by top-level func check
          "../config[contains(s,'x')]/enable = 'true'" -> "../config[contains(s,'x')]/enable"
        """
        # We need to find the first operator that is NOT inside brackets or quotes
        depth_bracket = 0
        depth_paren = 0
        in_single = False
        in_double = False
        i = 0
        while i < len(expr):
            c = expr[i]
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if c == '[':
                    depth_bracket += 1
                elif c == ']':
                    depth_bracket -= 1
                elif c == '(':
                    depth_paren += 1
                elif c == ')':
                    depth_paren -= 1
                elif depth_bracket == 0 and depth_paren == 0:
                    # Check for operators: =, !=, <=, >=, <, >
                    if c in ('=', '<', '>'):
                        break
                    if c == '!' and i + 1 < len(expr) and expr[i+1] == '=':
                        break
                    # Check for ' and ' / ' or ' keyword operators
                    rest = expr[i:]
                    if re.match(r'\s+(and|or)\s+', rest, re.IGNORECASE):
                        break
            i += 1
        return expr[:i].strip()

    def _extract_predicate_paths(self, path_expr: str) -> List[str]:
        """
        Extract path expressions from inside XPath predicates [...].

        For: "acl-set[name=current()/../set-name][type=current()/../type]/seq-id"
        Returns: ["__CONTEXT__/../set-name", "__CONTEXT__/../type"]

        For: "config[contains(services, 'oc-gnsi:GNSI')]/enable"
        Returns: [] (contains() has no resolvable path)
        """
        paths = []
        # Find all [...] blocks
        i = 0
        while i < len(path_expr):
            if path_expr[i] == '[':
                # Find matching ]
                depth = 1
                j = i + 1
                while j < len(path_expr) and depth > 0:
                    if path_expr[j] == '[':
                        depth += 1
                    elif path_expr[j] == ']':
                        depth -= 1
                    j += 1
                predicate_content = path_expr[i+1:j-1].strip()
                # Extract path from predicate: key=path or just path
                pred_path = self._extract_path_from_predicate(predicate_content)
                if pred_path:
                    paths.append(pred_path)
                i = j
            else:
                i += 1
        return paths

    def _extract_path_from_predicate(self, predicate: str) -> Optional[str]:
        """
        Extract the path argument from a predicate expression.

        Examples:
          "name=current()/../set-name"  -> "current()/../set-name"
          "mode-id=current()"           -> "current()"
          "contains(services, 'GNSI')"  -> None (no resolvable path)
          "protocol=current()/../src-protocol" -> "current()/../src-protocol"
        """
        # Check if it's a function call with no path (e.g. contains(...))
        func_match = re.match(r'^([a-zA-Z][a-zA-Z0-9_-]*)\s*\(', predicate)
        if func_match:
            fname = func_match.group(1)
            if fname in self.NO_PATH_FUNCTIONS:
                return None

        # Look for key=value pattern
        eq_match = re.match(r'^[^=\[]+\s*=\s*(.+)$', predicate, re.DOTALL)
        if eq_match:
            rhs = eq_match.group(1).strip()
            # If RHS is a quoted literal, no path
            if (rhs.startswith("'") and rhs.endswith("'")) or \
               (rhs.startswith('"') and rhs.endswith('"')):
                return None
            # RHS is a path expression (possibly with current())
            return rhs

        # No = sign — the whole predicate is a path/boolean expression
        # e.g. [mode-id] or [current()]
        if re.match(r'^[a-zA-Z_\./\-:]+$', predicate) or 'current()' in predicate:
            return predicate

        return None

    def _strip_predicates(self, path_expr: str) -> str:
        """
        Remove all [...] predicate blocks from a path expression, keeping
        the structural path.

        "acl-set[name=current()/../set-name][type=x]/seq-id"
        -> "acl-set/seq-id"

        "../../operational-modes[mode-id=current()]/mode-id"
        -> "../../operational-modes/mode-id"
        """
        result = []
        i = 0
        while i < len(path_expr):
            if path_expr[i] == '[':
                depth = 1
                i += 1
                while i < len(path_expr) and depth > 0:
                    if path_expr[i] == '[':
                        depth += 1
                    elif path_expr[i] == ']':
                        depth -= 1
                    i += 1
            else:
                result.append(path_expr[i])
                i += 1
        return ''.join(result)

    def _resolve_context_placeholder(self, path: str, context_path: str) -> str:
        """
        Replace __CONTEXT__ placeholder with the actual context path navigation.

        __CONTEXT__ represents current() which is the context node itself.

        Cases:
          "__CONTEXT__/../../../../config/name"
            -> navigate from context_path down ../../../../config/name
            -> equivalent to "../../../../config/name" from context
          "__CONTEXT__/../set-name"
            -> equivalent to "../set-name" from context
          "__CONTEXT__"
            -> the context node itself (empty relative path = '.')
        """
        if '__CONTEXT__' not in path:
            return path

        # Split on __CONTEXT__
        parts = path.split('__CONTEXT__')
        if len(parts) != 2:
            # Multiple current() — handle first one only
            path = path.replace('__CONTEXT__', '.', 1)
            path = path.replace('__CONTEXT__', '.')
            return path

        before = parts[0]  # Should be empty or a path prefix
        after = parts[1]   # The path after current()

        # after starts with '/' if current()/foo, or is empty if just current()
        if after.startswith('/'):
            # current()/foo/bar — navigate from context DOWN
            # This means: start at context, go to foo/bar
            # Equivalent to ./foo/bar from context
            after_path = after[1:]  # Remove leading /
            if before.strip('/'):
                # Something before current() — unusual, treat as-is
                return before + context_path + '/' + after_path
            return './' + after_path if after_path else '.'
        elif after == '':
            # Just current() alone — the context node itself
            return '.'
        else:
            # current() followed by something without / — shouldn't happen
            return '.' + after

    def _strip_prefixes(self, path: str) -> str:
        """
        Remove YANG namespace prefixes from path components.

        "oc-acl:acl-set/oc-acl:name" -> "acl-set/name"
        "oc-if:interface[oc-if:name=current()/../interface]"
        -> "interface[name=current()/../interface]"
        """
        return self._re_prefix_strip.sub('', path)

    def _split_first_arg(self, args_str: str) -> Tuple[str, str]:
        """
        Split a function argument string at the first top-level comma.

        "../type, 'oc-platform-types:TRANSCEIVER'"
        -> ("../type", " 'oc-platform-types:TRANSCEIVER'")
        """
        depth_paren = 0
        depth_bracket = 0
        in_single = False
        in_double = False
        for i, c in enumerate(args_str):
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if c == '(':
                    depth_paren += 1
                elif c == ')':
                    depth_paren -= 1
                elif c == '[':
                    depth_bracket += 1
                elif c == ']':
                    depth_bracket -= 1
                elif c == ',' and depth_paren == 0 and depth_bracket == 0:
                    return args_str[:i], args_str[i+1:]
        # No comma found — return whole string as first arg
        return args_str, ''


# ---------------------------------------------------------------------------
# High-level resolver that integrates with the base XPathResolver
# ---------------------------------------------------------------------------

class FunctionAwareXPathResolver:
    """
    Augments the base XPathResolver with RFC 7950 built-in function support.

    This class wraps XPathFunctionParser to pre-process XPath expressions
    before handing them to the base path navigation logic.

    Usage:
        resolver = FunctionAwareXPathResolver()
        result = resolver.resolve(
            context_path="/module/container/leaf",
            xpath_expr="current()/../../../../config/name"
        )
        print(result.absolute_path)  # -> "../../config/name" (relative)
        # Then pass result.primary_path to base XPathResolver.resolve_xpath()
    """

    def __init__(self):
        self._parser = XPathFunctionParser()

    def _is_malformed_absolute_path(self, path: str) -> bool:
        """Return True when an absolute path contains '.' or '..' segments."""
        parts = [p for p in path.split('/') if p]
        return any(part in {'.', '..'} for part in parts)

    def preprocess(
        self,
        context_path: str,
        xpath_expr: str
    ) -> FunctionParseResult:
        """
        Pre-process an XPath expression, resolving built-in functions to
        their path arguments.

        This is the main entry point. The returned FunctionParseResult
        contains primary_path which can be passed directly to the base
        XPathResolver.resolve_xpath() method.

        Args:
            context_path: Absolute schema path of the context node
            xpath_expr:   Raw XPath expression from when/must/leafref

        Returns:
            FunctionParseResult with primary_path ready for base resolver
        """
        return self._parser.parse(xpath_expr, context_path)

    def resolve_relative_path(
        self,
        context_path: str,
        relative_path: str
    ) -> str:
        """
        Resolve a relative path (with ../ steps) from a context path to
        an absolute schema path.

        This replicates the core navigation logic of XPathResolver.resolve_xpath()
        so this module can be used standalone (without loading a YANG file).

        Args:
            context_path:  Absolute path e.g. "/module/container/list/leaf"
            relative_path: Relative path e.g. "../../config/type" or
                           "./config/type" or "/absolute/path"

        Returns:
            Absolute path string, or empty string on error
        """
        path = relative_path.strip()

        # Absolute path — accept only structural node steps.
        if path.startswith('/'):
            if self._is_malformed_absolute_path(path):
                return ''
            return path

        # Current node
        if path == '.':
            return context_path

        # Remove ./ prefix
        if path.startswith('./'):
            path = path[2:]

        # Count and consume ../ steps
        parent_steps = 0
        while path.startswith('../'):
            parent_steps += 1
            path = path[3:]
        # Handle trailing .. (no trailing slash)
        if path == '..':
            parent_steps += 1
            path = ''

        # Navigate up from context
        parts = [p for p in context_path.split('/') if p]
        if parent_steps >= len(parts):
            # Can't go above root
            return ''

        parts = parts[:-parent_steps] if parent_steps > 0 else parts

        # Append forward path
        if path:
            forward_parts = [p for p in path.split('/') if p]
            parts.extend(forward_parts)

        return '/' + '/'.join(parts)

    def full_resolve(
        self,
        context_path: str,
        xpath_expr: str
    ) -> ResolvedFunctionPath:
        """
        Full resolution: parse functions + resolve relative path to absolute.

        This is a standalone resolver that does NOT require a loaded YANG
        module. It resolves the structural path only (no node lookup).

        Args:
            context_path: Absolute schema path of the context node
            xpath_expr:   Raw XPath expression

        Returns:
            ResolvedFunctionPath with absolute_path
        """
        parse_result = self._parser.parse(xpath_expr, context_path)

        if parse_result.no_path:
            return ResolvedFunctionPath(
                absolute_path='',
                context_path=context_path,
                original_xpath=xpath_expr,
                parse_result=parse_result,
                success=True,  # Not an error — just no path
                error=None
            )

        if parse_result.error:
            return ResolvedFunctionPath(
                absolute_path='',
                context_path=context_path,
                original_xpath=xpath_expr,
                parse_result=parse_result,
                success=False,
                error=parse_result.error
            )

        primary = parse_result.primary_path
        if not primary:
            return ResolvedFunctionPath(
                absolute_path='',
                context_path=context_path,
                original_xpath=xpath_expr,
                parse_result=parse_result,
                success=True,
                error=None
            )

        abs_path = self.resolve_relative_path(context_path, primary)
        if not abs_path:
            return ResolvedFunctionPath(
                absolute_path='',
                context_path=context_path,
                original_xpath=xpath_expr,
                parse_result=parse_result,
                success=False,
                error=(
                    "Path is invalid or navigates above schema root: "
                    f"{primary}"
                )
            )

        # Also resolve secondary (predicate) paths
        secondary = []
        for pred_path in parse_result.predicate_paths:
            resolved = self.resolve_relative_path(context_path, pred_path)
            if resolved:
                secondary.append(resolved)

        return ResolvedFunctionPath(
            absolute_path=abs_path,
            context_path=context_path,
            original_xpath=xpath_expr,
            parse_result=parse_result,
            success=True,
            secondary_paths=secondary
        )


# ---------------------------------------------------------------------------
# Integration helper: augment the base XPathResolver
# ---------------------------------------------------------------------------

def augment_xpath_resolver_resolve(
    context_path: str,
    xpath_expr: str,
    base_resolver_fn,
    function_resolver: Optional[FunctionAwareXPathResolver] = None
):
    """
    Drop-in augmentation for XPathResolver.resolve_xpath().

    Pre-processes the XPath expression to handle built-in functions, then
    delegates to the base resolver function for actual node lookup.

    Args:
        context_path:      Absolute schema path of the context node
        xpath_expr:        Raw XPath expression
        base_resolver_fn:  Callable matching XPathResolver.resolve_xpath(ctx, xpath)
        function_resolver: Optional pre-created FunctionAwareXPathResolver

    Returns:
        Whatever base_resolver_fn returns, but with function-aware path
    """
    if function_resolver is None:
        function_resolver = FunctionAwareXPathResolver()

    parse_result = function_resolver.preprocess(context_path, xpath_expr)

    if parse_result.no_path or not parse_result.primary_path:
        # No resolvable path — call base with original expression
        return base_resolver_fn(context_path, xpath_expr)

    # Call base resolver with the cleaned primary path
    return base_resolver_fn(context_path, parse_result.primary_path)


def compare_xpaths_with_functions(
    context_path: str,
    old_xpath: str,
    new_xpath: str,
    resolver: Optional[FunctionAwareXPathResolver] = None
) -> Tuple[str, str, bool, str]:
    """
    Compare two XPath expressions (possibly containing built-in functions)
    by resolving both to absolute schema paths.

    This is a standalone comparison that does NOT require a loaded YANG
    module — it performs structural path resolution only.

    Args:
        context_path: Absolute schema path of the context node
        old_xpath:    Old XPath expression
        new_xpath:    New XPath expression
        resolver:     Optional pre-created resolver (for efficiency)

    Returns:
        Tuple of (old_abs_path, new_abs_path, same_target, explanation)
    """
    if resolver is None:
        resolver = FunctionAwareXPathResolver()

    old_result = resolver.full_resolve(context_path, old_xpath)
    new_result = resolver.full_resolve(context_path, new_xpath)

    old_path = old_result.absolute_path
    new_path = new_result.absolute_path

    old_invalid = (not old_result.success) and not (
        old_result.parse_result and old_result.parse_result.no_path
    )
    new_invalid = (not new_result.success) and not (
        new_result.parse_result and new_result.parse_result.no_path
    )

    # Handle no-path cases (e.g. re-match)
    if old_result.parse_result and old_result.parse_result.no_path:
        old_path = '<no-path-function>'
    if new_result.parse_result and new_result.parse_result.no_path:
        new_path = '<no-path-function>'

    if old_invalid:
        old_path = '<invalid-path>'
    if new_invalid:
        new_path = '<invalid-path>'

    same = (old_path == new_path) and bool(old_path)

    if same:
        explanation = f"Both XPaths resolve to same schema path: {old_path}"
    elif old_invalid and new_invalid:
        explanation = (
            "Both XPaths are invalid (malformed or over-navigation):\n"
            f"  Old error: {old_result.error}\n"
            f"  New error: {new_result.error}"
        )
    elif old_invalid:
        explanation = f"Old XPath is invalid; new resolves to: {new_path}"
    elif new_invalid:
        explanation = f"New XPath is invalid; old resolves to: {old_path}"
    elif not old_path and not new_path:
        explanation = "Neither XPath contains a resolvable schema path"
        same = True  # Both are value-only expressions — treat as equivalent
    elif not old_path:
        explanation = f"Old XPath has no resolvable path; new resolves to: {new_path}"
    elif not new_path:
        explanation = f"New XPath has no resolvable path; old resolves to: {old_path}"
    else:
        explanation = (
            f"XPaths resolve to DIFFERENT schema paths:\n"
            f"  Old: {old_xpath!r}\n"
            f"       -> {old_path}\n"
            f"  New: {new_xpath!r}\n"
            f"       -> {new_path}\n"
            f"  Functions found (old): {old_result.parse_result.functions_found if old_result.parse_result else []}\n"
            f"  Functions found (new): {new_result.parse_result.functions_found if new_result.parse_result else []}"
        )

    return old_path, new_path, same, explanation


# ---------------------------------------------------------------------------
# Demo / manual test when run directly
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    resolver = FunctionAwareXPathResolver()

    # Real examples from OpenConfig YANG files
    EXAMPLES = [
        # (description, context_path, xpath_expr, expected_primary_path)

        # Phase 1: current() as path root
        (
            "current() as root — forward navigation",
            "/openconfig-vlan/interfaces/interface/routed-vlan",
            "current()/oc-if:config/oc-if:type = 'ianaift:l3ipvlan'",
            "./config/type",
        ),
        # Phase 1: current() inside predicate — leafref
        (
            "current() inside predicate — leafref path",
            "/openconfig-acl/acl/interfaces/interface/ingress-acl-sets/ingress-acl-set/acl-entries/acl-entry/state/sequence-id",
            "/oc-acl:acl/oc-acl:acl-sets/oc-acl:acl-set[oc-acl:name=current()/../../../../set-name][oc-acl:type=current()/../../../../type]/oc-acl:acl-entries/oc-acl:acl-entry/oc-acl:sequence-id",
            "/oc-acl:acl/oc-acl:acl-sets/oc-acl:acl-set/oc-acl:acl-entries/oc-acl:acl-entry/oc-acl:sequence-id",
        ),
        # Phase 1: current() as self-reference in predicate
        (
            "current() as self-reference in must predicate",
            "/openconfig-terminal-device/terminal-device/logical-channels/channel/logical-channel-assignments/assignment/config/optical-channel",
            "../../../../../../operational-mode-descriptors/operational-modes[mode-id=current()]/mode-id",
            "../../../../../../operational-mode-descriptors/operational-modes/mode-id",
        ),
        # Phase 1: current() with parent navigation in predicate
        (
            "current()/../sibling in predicate — leafref",
            "/openconfig-catalog/organizations/organization/modules/module/release-bundles/release-bundle/version",
            "release-bundle[name=current()/../name]/version",
            "release-bundle/version",
        ),
        # Phase 1: not() wrapper
        (
            "not() wrapper around path expression",
            "/module/container/leaf",
            "not(../config/enabled)",
            "../config/enabled",
        ),
        # Phase 1: contains() in predicate — no path from contains itself
        (
            "contains() in predicate — path is the node before predicate",
            "/openconfig-gnsi/grpc-servers/grpc-server/gnsi/authz/state",
            "../config[contains(services, 'oc-gnsi:GNSI')]/enable = 'true'",
            "../config/enable",
        ),
        # Phase 2: derived-from()
        (
            "derived-from() — extract path arg",
            "/module/components/component/state/type",
            "derived-from(../type, 'oc-platform-types:TRANSCEIVER')",
            "../type",
        ),
        # Phase 2: derived-from-or-self()
        (
            "derived-from-or-self() — extract path arg",
            "/module/components/component/state/type",
            "derived-from-or-self(../type, 'oc-platform-types:PORT')",
            "../type",
        ),
        # Phase 2: count()
        (
            "count() — extract node-set path",
            "/module/container/list",
            "count(../items) > 0",
            "../items",
        ),
        # Phase 3: enum-value()
        (
            "enum-value() — extract node-set path",
            "/module/container/leaf",
            "enum-value(../state/type) > 3",
            "../state/type",
        ),
        # Phase 3: bit-is-set()
        (
            "bit-is-set() — extract node-set path and bit name",
            "/module/container/leaf",
            "bit-is-set(../flags, 'UP')",
            "../flags",
        ),
        # Phase 3: deref()
        (
            "deref() — extract leafref source path",
            "/module/container/leaf",
            "deref(../interface-ref)/config/type",
            "../interface-ref",
        ),
        # Phase 3: re-match() — no path
        (
            "re-match() — no resolvable schema path",
            "/module/container/leaf",
            "re-match(../name, '[a-z]+')",
            "",
        ),
        # Plain relative path (no functions) — should work unchanged
        (
            "Plain relative path — no functions",
            "/openconfig-aaa/aaa/server-groups/server-group/servers/server/tacacs",
            "../../../config/type = 'oc-aaa:TACACS'",
            "../../../config/type",
        ),
        # Absolute path — no functions
        (
            "Absolute path — no functions",
            "/module/container/leaf",
            "/openconfig-mpls/lsps/constrained-path/tunnels/tunnel/config/type",
            "/openconfig-mpls/lsps/constrained-path/tunnels/tunnel/config/type",
        ),
    ]

    print("=" * 70)
    print("XPath Function Resolver — Demo")
    print("=" * 70)

    passed = 0
    failed = 0

    for desc, ctx, xpath, expected_primary in EXAMPLES:
        result = resolver.full_resolve(ctx, xpath)
        parse = result.parse_result

        # For comparison, check primary_path matches expected
        actual_primary = parse.primary_path if parse else ''
        # Strip prefixes from expected for comparison
        parser = XPathFunctionParser()
        expected_clean = parser._strip_prefixes(expected_primary)
        actual_clean = parser._strip_prefixes(actual_primary)

        ok = (actual_clean == expected_clean)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1

        print(f"\n[{status}] {desc}")
        print(f"  Context:  {ctx}")
        print(f"  XPath:    {xpath}")
        print(f"  Primary:  {actual_primary!r}")
        if parse:
            print(f"  Functions:{parse.functions_found}")
            if parse.predicate_paths:
                print(f"  Pred paths:{parse.predicate_paths}")
            if parse.identity_arg:
                print(f"  Identity: {parse.identity_arg!r}")
            if parse.bit_name_arg:
                print(f"  Bit name: {parse.bit_name_arg!r}")
            if parse.no_path:
                print(f"  No path:  True")
        print(f"  Abs path: {result.absolute_path!r}")
        if not ok:
            print(f"  EXPECTED: {expected_primary!r}")

    print(f"\n{'=' * 70}")
    print(f"Results: {passed} passed, {failed} failed out of {len(EXAMPLES)} tests")
    print("=" * 70)
