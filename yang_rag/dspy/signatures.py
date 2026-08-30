"""
DSPy Signatures for YANG Compatibility Analysis and Rule Generation
--------------------------------------------------------------------
All DSPy Signature classes live here — one canonical location.

Sections:
  1. Rule Generation Signatures  — used by RuleGeneratorModule (modules.py / pipeline.py)
  2. Verification Signatures     — used by CompatibilityVerifier and ConditionAnalyzer (modules.py)
"""

import dspy
from dspy import Signature, InputField, OutputField


class GenerateCompatibilityRule(Signature):
    """Generate XML compatibility rule for unknown YANG keyword based on similar keyword.
    
    The rule MUST follow the compatibility_rules.xml schema:
    - <rule> containing <rule-id>, ONE category element, <actions>, <compatible>
    - Category element (choose exactly ONE based on keyword type):
        * <structurals><structural>keyword</structural></structurals>  — for structural/block keywords
        * <constraints><constraint>keyword</constraint></constraints>  — for constraint keywords
        * <attributes><attribute>keyword</attribute></attributes>      — for attribute keywords
    - DO NOT use <keywords> or <keyword> — the schema uses <structurals>/<structural> instead
    - Actions: added, changed, deleted, with optional parent attribute
    - Compatible: backward-compatible, non-backward-compatible, conditional-backward-compatible
    """
    
    # Inputs
    unknown_keyword: str = InputField(
        desc="The unknown YANG keyword/extension that needs a compatibility rule"
    )
    similar_keyword: str = InputField(
        desc="Most similar known YANG keyword from RAG semantic search"
    )
    similarity_score: float = InputField(
        desc="Semantic similarity score between unknown and similar keyword (0.0-1.0)"
    )
    category: str = InputField(
        desc="YANG category of the similar keyword: structural, constraint, or attribute"
    )
    existing_rules: str = InputField(
        desc="XML rules from compatibility_rules.xml that apply to the similar keyword"
    )
    yang_context: str = InputField(
        desc="Example YANG syntax showing how the unknown keyword is used"
    )
    
    # Outputs
    generated_xml: str = OutputField(
        desc="Complete XML rule(s) in compatibility_rules.xml format. Must be valid XML with proper structure."
    )
    confidence: float = OutputField(
        desc="Confidence score (0.0-1.0) that this generated rule is appropriate"
    )
    rationale: str = OutputField(
        desc="Explanation of why this rule structure was chosen and how it relates to the similar keyword"
    )


class GenerateAllCompatibilityRules(Signature):
    """Generate ALL XML compatibility rules for an unknown keyword based on all existing rule patterns.
    
    Generate one rule for each pattern found in existing_rules (e.g., add, delete, relaxed change, narrowed change, etc.).
    
    CRITICAL: Use the correct XML schema:
    - Structural keywords: <structurals><structural>name</structural></structurals>
    - Constraint keywords: <constraints><constraint>name</constraint></constraints>
    - Attribute keywords:  <attributes><attribute>name</attribute></attributes>
    - DO NOT use <keywords>/<keyword> — the schema was updated to use <structurals>/<structural>
    """
    
    # Inputs
    unknown_keyword: str = InputField(
        desc="The unknown YANG keyword/extension that needs compatibility rules"
    )
    similar_keyword: str = InputField(
        desc="Most similar known YANG keyword from RAG semantic search"
    )
    similarity_score: float = InputField(
        desc="Semantic similarity score between unknown and similar keyword (0.0-1.0)"
    )
    category: str = InputField(
        desc="YANG category of the similar keyword: structural, constraint, or attribute"
    )
    existing_rules: str = InputField(
        desc="ALL XML rules from compatibility_rules.xml that apply to the similar keyword. Generate one new rule for each pattern."
    )
    yang_context: str = InputField(
        desc="Example YANG syntax showing how the unknown keyword is used"
    )
    
    # Outputs
    all_generated_rules: str = OutputField(
        desc="ALL XML rules generated (one for each existing rule pattern). Wrap multiple <rule> elements in a <rules> container."
    )
    rules_count: int = OutputField(
        desc="Number of rules generated (should match number of existing rules)"
    )
    overall_confidence: float = OutputField(
        desc="Overall confidence score (0.0-1.0) for the complete rule set"
    )
    rationale: str = OutputField(
        desc="Explanation of the complete rule set and how it covers different scenarios"
    )


class ExplainCompatibilityRule(Signature):
    """Explain compatibility rule(s) in natural language for developers.
    
    If multiple rules are provided, explain each one separately and clearly.
    Be accurate about backward compatibility based on each rule's <compatible> tag.
    """
    
    xml_rule: str = InputField(
        desc="XML compatibility rule(s). May contain single <rule> or multiple <rule> elements in <rules> container."
    )
    keyword: str = InputField(
        desc="The YANG keyword this rule applies to"
    )
    
    explanation: str = OutputField(
        desc="Clear explanation of what each rule means. For multiple rules, explain each separately with its rule-id and whether it's backward-compatible or not. Be precise about which actions are compatible and which are not."
    )


class ValidateGeneratedRule(Signature):
    """Validate that a generated XML rule follows the schema and makes semantic sense."""
    
    generated_xml: str = InputField(desc="The generated XML rule to validate")
    schema_requirements: str = InputField(desc="Requirements from the XML schema")
    original_keyword: str = InputField(desc="The original unknown keyword")
    
    is_valid: bool = OutputField(desc="Whether the XML is valid and follows schema")
    issues: str = OutputField(desc="List of any validation issues found, or 'None' if valid")
    suggested_fix: str = OutputField(desc="Suggested correction if invalid, or 'N/A' if valid")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Verification Signatures
# ─────────────────────────────────────────────────────────────────────────────

class VerifyCompatibilityDecision(dspy.Signature):
    """Verify if a compatibility decision is correct based on YANG semantics.
    
    CRITICAL RULES FOR YANG COMPATIBILITY (applies to keywords, attributes, and constraints):
    
    1. ADDED ELEMENT (action='added', old_value empty, new_value has content):
       - **SPECIAL CASE: If parent_context='parent_added'** → BACKWARD-COMPATIBLE
         * The constraint/attribute is added WITH its parent element (type/leaf/container)
         * Entire parent node is new, no existing clients depend on it
         * Example: Adding pattern to a newly added leaf is safe
         * In reasoning: Mention new_value and parent_added context, do NOT mention old_value
       - **WITHOUT parent added**: Depends on element type
         * CONSTRAINTS (pattern, range, must): NARROWS values on existing element → NON-BACKWARD-COMPATIBLE
           Example: Adding pattern '[0-9]+' to existing leaf rejects previously valid non-numeric strings
         * KEYWORDS (leaf, container): Usually BACKWARD-COMPATIBLE if optional
         * ATTRIBUTES (mandatory, config): Depends on specific attribute
         * In reasoning: Mention new_value and how it narrows the model, do NOT mention old_value
    
    2. DELETED ELEMENT (action='deleted', old_value has content, new_value empty):
       - CONSTRAINTS: RELAXES acceptable values → BACKWARD-COMPATIBLE
         * Example: Removing pattern '[0-9]+' allows any string (more permissive)
       - KEYWORDS: Usually NON-BACKWARD-COMPATIBLE (removes functionality)
         * Example: Deleting leaf breaks clients expecting that data
       - ATTRIBUTES: Depends on specific attribute
         * Example: Removing mandatory=true is BACKWARD-COMPATIBLE (less restrictive)
       - In reasoning: Mention old_value being removed and impact, do NOT mention new_value
    
    3. CHANGED ELEMENT (action='changed', both old_value and new_value present):
       - **FUNDAMENTAL COMPATIBILITY RULE**: A change is BACKWARD-COMPATIBLE if and only if:
         * OLD VALID SET ⊆ NEW VALID SET (old valid configurations are a SUBSET of new valid configurations)
         * In other words: Every configuration that was VALID under old constraint is STILL VALID under new constraint
         * This is NOT just about "relaxation" - the old must be completely contained in the new
       - Compare old_value vs new_value to determine if NARROWING or RELAXING:
         * NARROWED (more restrictive, smaller valid set) → NON-BACKWARD-COMPATIBLE (breaks existing valid data)
         * RELAXED (less restrictive, larger valid set) → Only BC if OLD ⊆ NEW, otherwise NON-BC
       - Examples:
         * Constraint: pattern '[0-9]+' → '[0-9]{3}' = NARROWED (requires 3+ digits) → NON-BC
         * Constraint: range '1..100' → '1..50' = NARROWED (smaller range) → NON-BC
         * Constraint: pattern '[0-9]{3}' → '[0-9]+' = RELAXED and OLD ⊆ NEW (3+ digits ⊆ any digits) → BC
       - **CRITICAL FOR LOGICAL CONDITIONS (when/must with boolean expressions)**:
         * ALWAYS construct a complete TRUTH TABLE comparing old vs new conditions
         * A change is BACKWARD-COMPATIBLE ⟺ For EVERY case where old=TRUE (accepts), new=TRUE also (still accepts)
         * A change is NON-BACKWARD-COMPATIBLE ⟺ There EXISTS any case where old=TRUE but new=FALSE (old accepts but new rejects)
         * Pay special attention to NEGATIONS - they reverse acceptance:
           - not(A and B) ≡ (not A) or (not B)  [De Morgan's Law - these ARE equivalent]
           - not(A and B) ≠ (A or not B)  [These are NOT equivalent - COMMON ERROR!]
         * **WORKED EXAMPLE - Study this carefully**:
           - Old: not(x='A' and y='B')  means "accept UNLESS both x='A' AND y='B'"
           - New: x='A' or y≠'B'  means "accept if x='A' OR if y≠'B'"
           - Question: Is OLD ⊆ NEW? (Are all old-accepted configs still accepted by new?)
           - Truth table (TRUE means condition accepts the configuration):
             | x='A' | y='B' | Old: not(A∧B) | New: A∨¬B | Old⊆New? |
             |-------|-------|---------------|-----------|---------|
             | No    | No    | TRUE          | TRUE      | ✓       |
             | No    | Yes   | TRUE          | FALSE     | ✗ FAIL  | ← OLD ACCEPTS {x≠'A', y='B'} BUT NEW REJECTS!
             | Yes   | No    | TRUE          | TRUE      | ✓       |
             | Yes   | Yes   | FALSE         | TRUE      | N/A     | (old rejects, doesn't matter what new does)
           - Result: Row 2 violates OLD ⊆ NEW → NON-BACKWARD-COMPATIBLE
           - WRONG reasoning: "New is more relaxed" - This is irrelevant! New accepts {Yes,Yes} which old rejects,
             but that doesn't make it BC. What matters is: Does new STILL accept everything old accepted?
           - CORRECT reasoning: "Old accepts {No, Yes} but new rejects it, so OLD ⊄ NEW" → NON-BC
         * Always verify with concrete examples: pick specific values and test both conditions
         * AVOID THE TRAP: "Relaxation" or "accepts more cases" does NOT mean BC if it also rejects old valid cases!
       - In reasoning: Mention both old_value and new_value, explain which is more restrictive

    4. **SPECIAL CASE: ENUM 'value' INTEGER CHANGES (keyword_or_constraint='value', keyword='enum')**:
       - RFC 7950 §9.6.5: The canonical/wire representation of an enum is its STRING LABEL (name), NOT its integer value.
       - In NETCONF (XML): <leaf>WPA3_ENTERPRISE</leaf>  — string label on the wire
       - In RESTCONF (JSON): "leaf": "WPA3_ENTERPRISE"  — string label on the wire
       - Therefore: Changing an enum's integer 'value' (e.g., 5 → 7) has NO EFFECT on:
         * NETCONF/RESTCONF data compatibility (wire format uses string labels)
         * 'when'/'must' XPath conditions that compare enum labels (e.g., "../opmode = 'WPA3_ENTERPRISE'")
         * leafref path resolution
       - Changing an enum's integer 'value' ONLY affects:
         * SNMP MIB encoding (which uses integers)
         * The XPath built-in function enum-value() which returns the integer
       - **DECISION RULE for enum 'value' integer changes**:
         * If the module uses enum-value() in any when/must → NON-BACKWARD-COMPATIBLE
         * If the module is NETCONF/RESTCONF only (no SNMP, no enum-value()) → BACKWARD-COMPATIBLE
         * OpenConfig modules are NETCONF/RESTCONF only → enum integer value changes are BACKWARD-COMPATIBLE
         * The string label (enum name) is unchanged → existing data and when/must conditions are unaffected
       - Example: WPA3_ENTERPRISE value 5→7 is BACKWARD-COMPATIBLE for NETCONF/RESTCONF clients
         because the wire format remains <opmode>WPA3_ENTERPRISE</opmode> in both old and new.
    
    Your task: Analyze old_value vs new_value for the given keyword/attribute/constraint and verify if original_decision is correct.
    Consider YANG RFC 7950 semantics and the specific nature of what's being changed.
    """

    # Context inputs
    rule_id: str = dspy.InputField(
        desc="The rule ID being applied (e.g., 'constraint-relaxed-change-rule', 'keyword-semantic-change-rule', 'attribute-value-change-rule')"
    )
    keyword_or_constraint: str = dspy.InputField(
        desc="The YANG element being evaluated - can be a keyword (leaf, container), attribute (mandatory, config), or constraint (pattern, must, range)"
    )
    action: str = dspy.InputField(
        desc="The action detected: 'added' (new element), 'changed' (modified element), or 'deleted' (removed element)"
    )
    original_decision: str = dspy.InputField(
        desc="Original compatibility decision from rule: 'backward-compatible', 'non-backward-compatible', or 'conditional-backward-compatible'. YOU MUST VALIDATE THIS BY COMPARING old_value VS new_value."
    )

    # Change details - CRITICAL FOR VERIFICATION
    old_value: str = dspy.InputField(
        desc="The old/original value. EMPTY STRING means element did NOT exist before (action='added'). If not empty, this is the previous value that must be compared against new_value to determine if the change narrows (more restrictive) or relaxes (less restrictive) the model."
    )
    new_value: str = dspy.InputField(
        desc="The new/modified value. EMPTY STRING means element was REMOVED (action='deleted'). If not empty, this is the new value that must be compared against old_value to determine if the change narrows or relaxes the model."
    )

    # YANG context
    yang_path: str = dspy.InputField(
        desc="Full YANG path where the change occurred (e.g., '/module:container/leaf')"
    )
    parent_context: str = dspy.InputField(
        desc="Context about the parent node: 'parent_added' means the parent element (type/leaf/container) was also added in this change, making child constraint additions backward-compatible. Empty string means parent already existed."
    )

    # Element type specifics (if applicable)
    constraint_type: str = dspy.InputField(
        desc="Type of element if applicable: 'regex' (pattern), 'numbers' (range, length), 'boolean' (config, mandatory), 'keyword' (structural), 'attribute' (metadata), or 'N/A'"
    )
    is_relaxed: bool = dspy.InputField(
        desc="True if the rule marks this as a relaxed change, False if narrowed. NOTE: This is the rule's initial classification - YOU MUST VERIFY by comparing old_value vs new_value. The rule might be wrong."
    )

    # Outputs
    verification_result: str = dspy.OutputField(
        desc="Verification result: 'CONFIRMED' (agree with original_decision), 'OVERRIDDEN' (disagree with original_decision), or 'UNCERTAIN' (needs human review). Base this on comparing old_value vs new_value."
    )
    confidence: float = dspy.OutputField(
        desc="Confidence score 0.0-1.0 for this verification decision"
    )
    verified_compatibility: str = dspy.OutputField(
        desc="YOUR verified compatibility based on old_value vs new_value analysis: 'backward-compatible' (relaxing change - accepts more), 'non-backward-compatible' (narrowing change - restricts existing valid data), or 'conditional-backward-compatible' (depends on usage). THIS MUST BE BASED ON ACTUAL VALUE COMPARISON, NOT THE RULE'S is_relaxed FLAG."
    )
    reasoning: str = dspy.OutputField(
        desc="Detailed explanation following this structure:\n"
        "- For ADDED (action='added'): 1) State the new_value being added (do NOT mention old_value), 2) If parent_context='parent_added', explain parent was also added making this backward-compatible, otherwise explain how adding this element narrows/restricts the model, 3) Compatibility verdict\n"
        "- For DELETED (action='deleted'): 1) State the old_value being removed (do NOT mention new_value), 2) Explain how removing this element relaxes/expands the model, 3) Compatibility verdict\n"
        "- For CHANGED (action='changed'): 1) State old_value and describe what it accepts, 2) State new_value and describe what it accepts, 3) **CRITICAL**: Check if OLD ⊆ NEW by verifying every configuration accepted by old is still accepted by new (construct truth table for boolean expressions), 4) If any old-valid case is now rejected by new → NON-BC, 5) Compatibility verdict with justification\n"
        "Always: Explain the semantic impact using SET INCLUSION logic (not just 'relaxation'), verify if original_decision is correct, cite YANG RFC 7950 if applicable. For boolean expressions, show at least one concrete example of a configuration to demonstrate your reasoning."
    )
    suggested_action: str = dspy.OutputField(
        desc="Suggested action: 'ACCEPT' (original_decision is correct), 'REVIEW' (original_decision is questionable, needs manual check), or 'REJECT' (original_decision is clearly wrong based on constraint analysis)"
    )


class AnalyzeConditionChange(dspy.Signature):
    """Analyze a YANG `when` or `must` condition change for backward compatibility.

    A change is BACKWARD-COMPATIBLE (RELAXED/EQUIVALENT) if and only if:
        OLD_VALID_SET ⊆ NEW_VALID_SET
    i.e. every configuration accepted by the OLD condition is STILL accepted by the NEW condition.

    A change is NON-BACKWARD-COMPATIBLE (NARROWED) if there EXISTS any configuration
    accepted by old but REJECTED by new.

    ── OPERAND VALIDITY FRAMEWORK ───────────────────────────────────────────────
    Each side of an equality condition has TWO operands:
      Left side  (path like "../config/type"): must resolve to a real schema node
      Right side (value like 'PROT_OTN' or PROT_OTN): must be a known identity/enum/bit

    A condition is VALID only if BOTH sides are valid.
    If EITHER side is unresolvable or undefined → condition is ALWAYS-FALSE.

    VALIDITY MATRIX:
      old_valid=False, new_valid=False → EQUIVALENT (both always-FALSE, same behavior)
      old_valid=False, new_valid=True  → RELAXED (BC): bug fix applied
      old_valid=True,  new_valid=False → NARROWED (NBC): break introduced
      old_valid=True,  new_valid=True  → check semantic equivalence of path+value

    ── SPECIAL CASE: UNQUOTED vs QUOTED IDENTIFIERS IN COMPARISONS ──────────────
    In XPath 1.0, string literals MUST be quoted: 'value' or "value".
    An unquoted identifier after '=' is a LOCATION PATH (node selector), NOT a string literal.

    RULE: An unquoted identifier that does NOT exist as a schema node or defined
    identity/enum/bit → evaluates to empty node-set → comparison is ALWAYS FALSE.

    BOTH-BROKEN CASE (most common):
      old="../config/protocol-type = prot-ethernet"  (prot-ethernet: undefined identity)
      new="../config/protocol-type = PROT_ETHERNET"  (PROT_ETHERNET: also undefined in module)
      → Both conditions are always-FALSE → EQUIVALENT (same behavior, both broken)
      NOTE: Even if PROT_ETHERNET is the "correct" name, if the path itself doesn't
      resolve (e.g., protocol-type doesn't exist), BOTH are always-FALSE → EQUIVALENT.

    FIX CASE (old broken, new valid):
      old="../config/type = bad-identity"  (undefined → always-FALSE)
      new="../config/type = 'PROT_OTN'"   (valid quoted identity → conditionally-TRUE)
      → RELAXED (BC): new accepts data that old rejected

    BREAK CASE (old valid, new broken):
      old="../config/type = 'PROT_OTN'"   (valid → conditionally-TRUE)
      new="../config/type = bad-identity"  (undefined → always-FALSE)
      → NARROWED (NBC): new rejects data that old accepted

    ── SPECIAL CASE: COMPARISON OPERATOR CHANGES ────────────────────────────────
    When the comparison operator changes but the threshold value stays the same:
      >= N → > N : NARROWED (strict excludes boundary value N)
      > N → >= N : RELAXED  (inclusive adds boundary value N)
      <= N → < N : NARROWED (strict excludes boundary value N)
      < N → <= N : RELAXED  (inclusive adds boundary value N)
      > N → < N  : INCOMPARABLE (direction inversion — requires deeper analysis)
      >= N → <= N: INCOMPARABLE (direction inversion — requires deeper analysis)

    ── SPECIAL CASE: NOT CLAUSE MIXED WITH NON-NOT ──────────────────────────────
    When one condition uses not() and the other doesn't:
      old="not(../enabled)"  new="../enabled = 'false'"
      → These may be logically equivalent (not(enabled) ≡ enabled='false' for boolean)
      → Use truth table to verify: construct all cases and check OLD ⊆ NEW
      old="not(x='A' and y='B')"  new="x='A' or y!='B'"
      → NOT equivalent! Use De Morgan's: not(A∧B) ≡ ¬A∨¬B, but x='A'∨y≠'B' ≠ ¬(x='A')∨¬(y='B')
      → Always construct truth table for NOT clause changes

    ── SPECIAL CASE: XPath RELATIVE-PATH DEPTH CHANGES ─────────────────────────
    A change from "../../config/type='X'" to "../../../config/type='X'" means the
    condition navigates to a DIFFERENT ancestor node. This is NOT simply relaxed or
    narrowed — it changes WHICH node is tested.
    → INCOMPARABLE (cannot determine without full schema tree)

    ── SPECIAL CASE: RFC 7950 §7.21.5 WRAPPER / ATTACHMENT CONTEXT (3 RULES) ──
    Rule 1 (Attachment Context Rule):
      Evaluate relative XPath from the node where the when/must is ATTACHED
      (effective owner), not from where the text appears in a diff line.
      Wrapper constructs (uses/choice/case/augment/refine) can shift attachment.

    Rule 2 (Wrapper Rewrite Equivalence Rule):
      If old/new path text differs due to wrapper-relative rewriting only
      (for example "type" ↔ "../type" or "config/type" ↔ "../config/type"),
      and both resolve to the SAME absolute schema node under the attachment
      context, classify as EQUIVALENT.

    Rule 3 (No-Guess Rule):
      If wrapper-aware resolution shows different absolute target nodes, or
      attachment-aware resolution is inconclusive, do NOT force narrowed/relaxed
      from text shape alone. Use INCOMPARABLE unless static evidence proves a
      deterministic validity transition (old broken → new valid = RELAXED,
      old valid → new broken = NARROWED).

    ── SPECIAL CASE: ENUM LABEL COMPARISONS IN when/must ────────────────────────
    YANG when/must conditions compare enum values using STRING LABELS, not integers.
    Per RFC 7950 §9.6.5, the canonical form of an enum is its name (string label).
    Changing an enum's integer 'value' (e.g., 5→7) does NOT affect string-label comparisons.
    Adding new enum labels to an OR chain: OLD ⊆ NEW → RELAXED (BC).

    ── SPECIAL CASE: NAMESPACE PREFIX QUALIFICATION ─────────────────────────────
    Adding/removing a namespace prefix on a comparison value:
      old="../config/type = 'L2P2P'"              → unqualified
      new="../config/type = 'oc-ni-types:L2P2P'"  → qualified with module prefix
      → EQUIVALENT: same identity, same schema path, same runtime behavior.
    Changing prefix (different module, same local name):
      → EQUIVALENT if modules are aliases; INCOMPARABLE if potentially different modules.

    ── RULES for logical/boolean condition changes ───────────────────────────────
    Use a truth table to check OLD ⊆ NEW:
      - If new condition is STRICTLY MORE PERMISSIVE for ALL inputs → RELAXED
      - If new condition is STRICTLY MORE RESTRICTIVE for ANY input → NARROWED
      - If conditions are logically equivalent → EQUIVALENT
      - If neither contains the other → INCOMPARABLE

    ── EXAMPLES ─────────────────────────────────────────────────────────────────
    1. old="../../config/type='ACL_IPV4'"  new="../../../config/type='ACL_IPV4'"
       → Path depth changed, different ancestor → INCOMPARABLE (HIGH)
    2. old="type='A' or type='B'"  new="type='A' or type='B' or type='C'"
       → New adds a case; OLD ⊆ NEW → RELAXED (HIGH)
    3. old="type='A' or type='B'"  new="type='A'"
       → New removes a case; OLD ⊄ NEW → NARROWED (HIGH)
    4. old="not(type='A')"  new="type!='A'"  → Logically equivalent → EQUIVALENT (HIGH)
    5. old="../config/protocol-type = prot-ethernet"
       new="../config/protocol-type = PROT_ETHERNET"
       → Both unquoted identifiers undefined AND path unresolvable → EQUIVALENT (HIGH)
       (Both conditions are always-FALSE; same behavior in both versions)
    6. old="../count >= 5"  new="../count > 5"
       → Operator tightened: >= includes 5, > excludes 5 → NARROWED (HIGH)
    7. old="../count > 5"  new="../count >= 5"
       → Operator relaxed: >= includes boundary value 5 → RELAXED (HIGH)
    8. old="not(x='A' and y='B')"  new="x='A' or y!='B'"
       → Truth table: {x≠A, y=B}: old=TRUE, new=FALSE → NARROWED (HIGH)
    9. old="../opmode='WPA3_ENTERPRISE' or ../opmode='WPA3_SAE'"
       new="../opmode='WPA3_ENTERPRISE' or ../opmode='WPA3_2_ENTERPRISE_TRANSITION' or ../opmode='WPA3_SAE'"
       → New adds enum label; OLD ⊆ NEW → RELAXED (HIGH)
    """

    old_condition = dspy.InputField(
        desc="Original YANG `when`/`must` XPath condition string. E.g. \"../../config/type='ACL_IPV4'\""
    )
    new_condition = dspy.InputField(
        desc="Modified YANG `when`/`must` XPath condition string. E.g. \"../../../config/type='ACL_IPV4'\""
    )
    context = dspy.InputField(
        desc="Additional context: yang_path (schema path of the node), constraint type, action. "
             "May also include wrapper/attachment hints from static analysis (effective context, "
             "fallback usage, anchor information). Use this to apply RFC 7950 §7.21.5 attachment semantics.",
        default=""
    )
    static_findings = dspy.InputField(
      desc=(
        "Structured findings from the static ConditionAnalyzer. Includes:\n"
        "  - preliminary_direction: the static tool's classification (EQUIVALENT/NARROWED/RELAXED/INCOMPARABLE/needs_llm_analysis)\n"
        "  - preliminary_explanation: the static tool's reasoning\n"
        "  - lhs_rhs: parsed left-hand side (path) and right-hand side (value) for old and new conditions\n"
        "  - lhs_rhs_validity: old_lhs_valid / old_rhs_valid / new_lhs_valid / new_rhs_valid (True/False/None)\n"
        "  - lhs_rhs_reasons: why each side was marked valid or invalid\n"
        "  - path_resolution_context: effective contexts and fallback usage for old/new resolution\n"
        "Use path_resolution_context + attachment hints to apply wrapper-aware RFC 7950 §7.21.5 rules "
        "before deciding narrowed/relaxed/equivalent/incomparable. "
        "You MUST validate each of these findings against the YANG module content and your own analysis. "
        "Explicitly state whether you CONFIRM or DISAGREE with the static tool's preliminary_direction."
      ),
      default=""
    )

    classification = dspy.OutputField(
        desc=(
            "Exactly one of: NARROWED | RELAXED | EQUIVALENT | INCOMPARABLE\n"
            "NARROWED = new condition rejects something old accepted (breaking)\n"
            "RELAXED  = new condition accepts everything old did PLUS more (safe)\n"
            "EQUIVALENT = conditions accept exactly the same set of configurations\n"
            "INCOMPARABLE = cannot determine set relationship without schema context "
            "(e.g. XPath depth-shift to different ancestor, or mixed predicate changes)"
        )
    )
    explanation = dspy.OutputField(
        desc=(
            "1-3 sentence explanation. Must state: (1) what old_condition checks, "
            "(2) what new_condition checks, (3) why the classification was chosen. "
            "For XPath depth changes, explicitly name the ancestor levels involved."
        )
    )
    confidence = dspy.OutputField(
        desc="Confidence level: HIGH (clear logical analysis), MEDIUM (some ambiguity), or LOW (insufficient context)"
    )
    outcome = dspy.OutputField(
        desc=(
            "Whether your classification CONFIRMS or DISAGREES with the static tool's preliminary_direction "
            "from static_findings. Exactly one of:\n"
            "  confirmed   — your classification matches the static tool's preliminary_direction\n"
            "  unconfirmed — your classification differs from the static tool's preliminary_direction\n"
            "If static_findings is empty or preliminary_direction is 'needs_llm_analysis', output 'confirmed' "
            "if you reached a definitive classification, otherwise 'unconfirmed'."
        )
    )


class JudgeExtensionCompatibility(Signature):
    """Independently judge whether the compatibility decision transferred from a known YANG
    keyword to an unknown vendor extension keyword is semantically appropriate.

    IMPORTANT: You must form your judgment based SOLELY on the semantic relationship between
    the extension keyword and the matched keyword — do NOT simply echo or be biased by the
    user_confidence value. The user_confidence is provided only as a secondary cross-check
    signal AFTER you have reached your own independent conclusion.

    Evaluate whether the BC/NBC rules of the matched keyword transfer correctly to the
    extension keyword by reasoning about:
      1. Whether the extension keyword controls the same kind of schema property as the matched keyword.
      2. Whether the direction of compatibility (relaxing vs. narrowing) maps correctly.
      3. Whether the decision is universally applicable or depends on deployment-specific client usage.

    Output 'aligned' if the inherited compatibility decision is appropriate regardless of
    deployment context, or 'uncertain' if the decision depends on how specific clients use
    the extension and cannot be determined without additional context.
    """

    extension_keyword: str = dspy.InputField(
        desc="The unknown vendor extension keyword (e.g. 'internal', 'display-hint', 'filter')"
    )
    extension_snippet: str = dspy.InputField(
        desc="Example YANG usage snippet showing how the extension keyword appears in a module"
    )
    matched_keyword: str = dspy.InputField(
        desc="The known YANG keyword selected as the best structural/semantic match from the corpus"
    )
    matched_rules_summary: str = dspy.InputField(
        desc="Summary of the compatibility rules inherited from the matched keyword "
             "(e.g. 'added→NBC, deleted→BC, changed with relaxed=true→BC')"
    )
    user_confidence: int = dspy.InputField(
        desc="Secondary cross-check only — do NOT use this as your primary judgment input. "
             "After forming your independent judgment, note whether it agrees or disagrees with "
             "the user: 1 = user believes the inherited rules apply directly, "
             "0 = user is uncertain about applicability. Your judgment may differ from the user's."
    )

    judgment: str = dspy.OutputField(
        desc="Your INDEPENDENT assessment. Exactly one of: "
             "'aligned' (the inherited compatibility decision is appropriate for this extension "
             "keyword based on semantic analysis, regardless of user_confidence) or "
             "'uncertain' (the decision depends on deployment-specific client usage semantics "
             "that cannot be determined from the keyword and snippet alone). "
             "Do NOT default to 'uncertain' just because user_confidence=0."
    )
    reasoning: str = dspy.OutputField(
        desc="Concise explanation (2-4 sentences) of your independent semantic reasoning. "
             "State whether your judgment agrees or disagrees with the user_confidence value "
             "and briefly explain why."
    )


class JudgeMappingQuality(Signature):
    """Judge whether the RAG-selected YANG keyword is a structurally and format-compatible match
    for the unknown vendor extension keyword, from the perspective of an XML rule engine.

    CRITICAL: This is NOT a semantic equivalence check. You are NOT asking whether the two
    keywords do the same thing or have the same behavioral purpose. You are asking whether
    the XML compatibility rule engine can handle the unknown extension keyword using the SAME
    rule structure as the matched keyword.

    The XML rule engine classifies changes based on:
      - Value format: string, boolean, number, regex, flag (no value), block (has children)
      - Action type: added, changed, deleted
      - Compatibility direction: relaxing (BC) or narrowing (NBC)

    Evaluate ONLY whether:
      1. The extension keyword has the SAME VALUE FORMAT as the matched keyword
         (e.g., both are string-valued, both are boolean flags, both are block constructs).
      2. The extension keyword follows the SAME STRUCTURAL PATTERN
         (e.g., both are leaf attributes, both are named blocks, both are flag-type).
      3. The XML rule for the matched keyword could be cloned and applied to the extension
         keyword without changing the rule's type, format, or action logic.

    EXAMPLES of correct 'agree' verdicts:
      - set-hook "node" → default "value": both are string-valued attributes; same format.
      - posix-pattern "^[0-9]+$" → pattern "[0-9]+": both are regex-valued constraints; same format.
      - sort-order "ordered" → ordered-by "system": both are enumeration-valued constraints; same format.
      - internal (no value) → mandatory-flag (no value): both are flag-type attributes; same format.

    EXAMPLES of correct 'disagree' verdicts:
      - oid "1.3.6.1" → name "foo": oid is a dotted-decimal identifier with specific format rules
        that differ from a generic name string; the rule logic would need modification.

    Output 'agree' if the structural format and rule logic are compatible, or 'disagree' if
    the format or rule structure is fundamentally incompatible.
    """

    extension_keyword: str = dspy.InputField(
        desc="The unknown vendor extension keyword (e.g. 'set-hook', 'posix-pattern', 'internal')"
    )
    extension_snippet: str = dspy.InputField(
        desc="Example YANG usage snippet showing the extension keyword's value format and structure"
    )
    matched_keyword: str = dspy.InputField(
        desc="The known YANG keyword selected by the RAG system as the best structural/format match"
    )
    matched_snippet: str = dspy.InputField(
        desc="Example usage snippet of the matched keyword, showing its value format and structure"
    )

    verdict: str = dspy.OutputField(
        desc="Exactly one of: 'agree' (the extension keyword follows the same structural format "
             "and the XML rule engine can handle it using the matched keyword's rule structure) or "
             "'disagree' (the format or rule structure is fundamentally incompatible and the rule "
             "would need significant modification beyond a simple clone)"
    )
    reasoning: str = dspy.OutputField(
        desc="Concise explanation (2-3 sentences) focused on VALUE FORMAT and STRUCTURAL PATTERN "
             "compatibility, not semantic purpose. State whether the formats match and whether "
             "the XML rule engine can apply the same rule logic."
    )


class JudgeRuleCloneReadiness(Signature):
    """Judge whether the XML compatibility rule of the matched YANG keyword can be cloned and
    applied to the unknown vendor extension keyword by ONLY substituting the keyword name,
    with no other changes to the rule's XML attributes.

    You are given the ACTUAL XML rule snippet for the matched keyword. Your task is to inspect
    the specific XML attributes present in that rule and determine whether any of them would
    need to be changed for the rule to work correctly with the extension keyword.

    The XML rule attributes that might need modification are:
      - type="numbers|regex|boolean|olist|ulist|xpath|flag|version|condition"
        → needs changing if the extension keyword has a different value type
      - relaxed="true|false"
        → needs changing if the narrowing/relaxing direction differs
      - separator=" "
        → needs changing if the extension keyword uses a different list separator
      - parent="container|leaf|..."
        → needs changing if the extension keyword appears in a different parent context
      - symbolic="true"
        → needs changing if the extension keyword does NOT require position/identity preservation
      - value="true|false|current|deprecated|..."
        → needs changing if the extension keyword has different specific value semantics
      - assistance="true"
        → needs changing if the extension keyword does NOT require LLM assistance for analysis

    MANDATORY FIRST STEP — READ ALL RULES BEFORE DRAWING ANY CONCLUSION:
    The matched_xml_rule field may contain MULTIPLE rules. You MUST read ALL of them before
    deciding. Do NOT evaluate rules one by one and stop at the first mismatch.
    Ask: "Does the COMPLETE SET of rules collectively cover the extension keyword's usage?"
    If ANY rule in the set handles the extension keyword's value/format/context → the set
    can be cloned as-is for that case. Only output 'needs-modification' if NO rule in the
    set covers the extension keyword's usage without attribute changes.
    Example: if one rule has value="true" and another has value="false", the SET covers
    both boolean values — do NOT say "the value="true" rule doesn't match false".

    SIMPLE RULES (no special attributes beyond keyword name):
    If the matched rule has NO special type/relaxed/separator/parent/symbolic attributes —
    just a plain added→BC, changed→NBC, deleted→NBC pattern — then the rule can ALWAYS be
    cloned as-is for ANY extension keyword regardless of its value format.
    Output 'clone-as-is' for these cases.
    CRITICAL: Do NOT suggest adding a type attribute to a rule that has none. A rule without
    a type attribute is intentionally general-purpose. Adding type="number" or any other type
    to a general rule would NARROW it unnecessarily. If the original rule works without a type
    attribute, the cloned rule should also work without one.

    COMPLEX RULES (has special attributes):
    If the matched rule has type="regex", type="numbers", type="olist", relaxed="true/false",
    separator, parent, or symbolic attributes, check whether those attributes still apply
    correctly to the extension keyword. If they do → 'clone-as-is'. If they need changing → 'needs-modification'.

    DECISION RULE FOR COMPLEX RULES — use the extension snippet's placeholder to decide:
      If extension snippet contains <REGEX>  AND rule has type="regex"  → clone-as-is
      If extension snippet contains <RANGE>  AND rule has type="numbers" → clone-as-is
      If extension snippet contains <BOOLEAN> AND rule has type="boolean" → clone-as-is
      If extension snippet contains <VERSION> AND rule has type="version" → clone-as-is
      If extension snippet contains <NO_VALUE> AND rule has type="flag"  → clone-as-is
      If extension snippet contains <STRING> or <IDENTIFIER> AND rule has no type → clone-as-is
      relaxed="true" or relaxed="false" does NOT need changing just because the keyword name differs —
        only change relaxed if the narrowing/relaxing direction is genuinely reversed for the extension.
      DO NOT output 'needs-modification' based on semantic purpose differences or keyword name differences.
      ONLY output 'needs-modification' if the placeholder type in the extension snippet is
      GENUINELY INCOMPATIBLE with the type attribute in the XML rule (e.g., <BOOLEAN> vs type="regex").

    The user has indicated their confidence (0 or 1) as a secondary cross-check:
      - conf=1: user believes the rule can be cloned with name substitution only
      - conf=0: user believes one or more XML attributes need changing

    Form your judgment INDEPENDENTLY by inspecting the actual XML rule. Do NOT echo the user's confidence.
    """

    extension_keyword: str = dspy.InputField(
        desc="The unknown vendor extension keyword (e.g. 'set-hook', 'posix-pattern', 'internal')"
    )
    extension_snippet: str = dspy.InputField(
        desc="Example YANG usage snippet showing the extension keyword's value format and usage. "
             "The snippet may contain a MASKED TEMPLATE placeholder (e.g. <BOOLEAN>, <VERSION>, <REGEX>, "
             "<STRING>, <IDENTIFIER>, <NUMBER>, <RANGE>, <NO_VALUE>). "
             "These placeholders DIRECTLY MAP to the type attribute in the XML rule:\n"
             "  <BOOLEAN>    → type=\"boolean\"  (true/false values)\n"
             "  <VERSION>    → type=\"version\"  (version strings like '1.1', '1.1.0')\n"
             "  <REGEX>      → type=\"regex\"    (regular expression strings; "
             "any value described as 'regular expression format' is equivalent to <REGEX>)\n"
             "  <NUMBER>     → type=\"number\"   (single numeric value; also supported by type=\"numbers\")\n"
             "  <RANGE>      → type=\"numbers\"  (numeric range like '0..100'; "
             "type=\"numbers\" supports BOTH <RANGE> and <NUMBER> values)\n"
             "  NO type attr → supports ANY value format including <RANGE>, <NUMBER>, <STRING>, etc. "
             "A rule with no type attribute is general-purpose and does NOT need a type added.\n"
             "  <STRING> or <IDENTIFIER> → plain string attribute (no special type needed)\n"
             "  <NO_VALUE> or bare keyword with no value → type=\"flag\"\n"
             "REGEX IDENTIFICATION: If the snippet or bare query contains a quoted string with "
             "regex metacharacters (^, $, [, ], {, }, *, +, ?, |, \\d, \\w, etc.), the value IS "
             "a regular expression and matches type=\"regex\". The placeholder may appear as "
             "<REGEX> or <STRING> — if the actual value looks like a regex, treat it as <REGEX>.\n"
             "CRITICAL: If the extension snippet's placeholder matches the type in the XML rule, "
             "the type attribute is CORRECT and does NOT need to be changed:\n"
             "  'ext-kw <VERSION>;' with rule type=\"version\" → clone-as-is\n"
             "  'ext-kw <BOOLEAN>;' or 'ext-kw true;' with rule type=\"boolean\" → clone-as-is\n"
             "  'ext-kw <REGEX>;' or 'ext-kw \"^[0-9]+$\";' with rule type=\"regex\" → clone-as-is\n"
             "  'ext-kw <RANGE>;' with rule type=\"numbers\" → clone-as-is\n"
             "  'ext-kw;' or 'ext-kw <NO_VALUE>;' with rule type=\"flag\" → clone-as-is\n"
             "BOOLEAN vs FLAG distinction:\n"
             "  FLAG:    keyword with NO value at all — just 'keyword;' or 'keyword <NO_VALUE>;'\n"
             "  BOOLEAN: keyword followed by 'true' or 'false' — 'keyword true;' or 'keyword <BOOLEAN>;'\n"
             "  If the snippet or bare query shows 'keyword true;' or 'keyword false;', it is BOOLEAN, "
             "NOT a flag. Do NOT confuse 'keyword true;' with a bare flag keyword.\n"
             "BOOLEAN VALUE EQUIVALENCES (these are identical — do NOT treat them as different):\n"
             "  value=\"true\"  in XML rule ↔ 'true' or <BOOLEAN> in extension snippet\n"
             "  value=\"false\" in XML rule ↔ 'false' or <BOOLEAN> in extension snippet\n"
             "  Both are boolean values. A rule with value=\"true\" applies when the extension "
             "keyword is set to true. A rule with value=\"false\" applies when it is set to false. "
             "They are NOT incompatible — they are complementary selectors for the same boolean type."
    )
    matched_keyword: str = dspy.InputField(
        desc="The known YANG keyword whose XML rule is being considered for cloning"
    )
    matched_xml_rule: str = dspy.InputField(
        desc="The ACTUAL XML rule snippet(s) for the matched keyword from compatibility_rules.xml. "
             "There may be MULTIPLE rules shown — inspect ALL of them. "
             "For each rule, check whether its specific attributes apply correctly to the extension keyword. "
             "Some rules may be clone-as-is while others need modification.\n"
             "ATTRIBUTE MEANINGS (do NOT change an attribute if the extension keyword matches it):\n"
             "  type=\"flag\"    → keyword takes NO value (bare keyword + semicolon). "
             "If extension keyword also has no value → type is CORRECT, do not change.\n"
             "  type=\"boolean\" → keyword takes true/false value. "
             "If extension keyword also takes true/false → type is CORRECT, do not change.\n"
             "  type=\"numbers\" → keyword takes numeric range (e.g. '0..100'). "
             "If extension keyword also takes a numeric range → type is CORRECT.\n"
             "  type=\"regex\"   → keyword takes a regular expression string. "
             "If extension keyword also takes a regex → type is CORRECT.\n"
             "  type=\"version\" → keyword takes a version string (e.g. '1.1', '1.1.0'). "
             "If extension keyword also takes a version string → type is CORRECT.\n"
             "  type=\"XPath\"   → keyword takes an XPath expression. "
             "If extension keyword takes a path/filter expression → type may be CORRECT.\n"
             "  value=\"true\"   → this specific rule applies when the keyword value IS 'true'. "
             "This is a RULE SELECTOR, not a constraint on the extension keyword's semantics. "
             "CRITICAL FOR MULTIPLE value-SELECTOR RULES: When you see multiple rules with "
             "value=\"true\" and value=\"false\" (and optionally a rule with no value selector), "
             "these rules TOGETHER cover ALL boolean states of the extension keyword. "
             "You MUST NOT say 'needs-modification' because one rule has value=\"false\" and "
             "the extension snippet shows 'true'. The value=\"false\" rule handles the case when "
             "the extension keyword is set to false — it is CORRECT as-is. "
             "The value=\"true\" rule handles the case when the extension keyword is set to true — "
             "it is also CORRECT as-is. The ENTIRE SET of rules can be cloned without modification. "
             "Output 'clone-as-is' when the matched keyword has multiple value-selector rules "
             "that collectively cover the extension keyword's boolean values.\n"
             "  relaxed=\"true\" → changing the value in the relaxing direction is BC. "
             "If the extension keyword also has a relaxing direction → relaxed is CORRECT.\n"
             "  separator=\"X\"  → the list items in the keyword's value are separated by character X. "
             "IMPORTANT: If the extension keyword uses a DIFFERENT separator than the matched rule, "
             "the separator attribute MUST be changed. "
             "Example: matched rule has separator=\" \" (space) but extension uses ';' → needs-modification. "
             "To identify the separator: look at the extension snippet value and find the character "
             "that separates the distinct items in the list (e.g., ';', ',', ' ', '|').\n"
             "  parent=\"X|Y\"   → this rule only applies when the keyword appears inside parent X or Y. "
             "If the extension keyword appears in a DIFFERENT parent context → parent needs changing.\n"
             "  assistance=\"true\" → this rule requires LLM assistance for analysis. "
             "If the extension keyword does NOT require LLM assistance → this attribute needs changing.\n"
             "IMPORTANT: Do NOT confuse path VALUES (e.g., '../type=\\'ipv4unicast\\'') with "
             "XML type ATTRIBUTES. A path value is just the string content of the keyword, "
             "not a type declaration.\n"
             "RULE: Only mark 'needs-modification' if a specific XML attribute needs to be changed. "
             "Do NOT change attributes based on semantic purpose differences."
    )
    user_confidence: int = dspy.InputField(
        desc="Secondary cross-check only — do NOT use as primary input. "
             "1 = user believes rule can be cloned with name substitution only; "
             "0 = user believes one or more XML attributes need changing. Your verdict may differ."
    )

    verdict: str = dspy.OutputField(
        desc="Exactly one of: 'clone-as-is' (the rule can be applied to the extension keyword "
             "by name substitution only — no XML attribute changes needed) or "
             "'needs-modification' (one or more XML attributes such as type, relaxed, separator, "
             "parent, or symbolic need to be changed before the rule applies correctly). "
             "Base this SOLELY on the XML rule attributes, not on semantic purpose."
    )
    reasoning: str = dspy.OutputField(
        desc="Concise explanation (2-4 sentences) referencing the SPECIFIC XML ATTRIBUTES in the "
             "matched rule(s). For each rule shown, state which attributes (if any) would need "
             "changing and why, or confirm that no attributes need changing. "
             "IMPORTANT: Do NOT confuse path values (string content like '../type=\\'x\\'') with "
             "XML type attributes. Only reference actual XML attributes (type=, relaxed=, parent=, etc.). "
             "When noting agreement with user_confidence: "
             "user_confidence=1 means 'clone as-is' and user_confidence=0 means 'needs modification'. "
             "Your verdict 'clone-as-is' AGREES with user_confidence=1 and DISAGREES with user_confidence=0. "
             "Your verdict 'needs-modification' AGREES with user_confidence=0 and DISAGREES with user_confidence=1."
    )
