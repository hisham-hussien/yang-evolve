# DSPy Module — LLM-Powered YANG Analysis

This module provides DSPy-based LLM capabilities for YANG compatibility analysis:

1. **Condition Analysis** — Verify `when`/`must` constraint changes (`AnalyzeConditionChange`)
2. **Compatibility Verification** — Double-check rule-based decisions for `assistance="true"` rules (`VerifyCompatibilityDecision`)
3. **Rule Generation** — Generate XML compatibility rules for unknown YANG keywords (`RuleGeneratorModule`)

> **DSPy Optimization Status — two separate systems:**
>
> | Component | Location | Optimizer |
> |---|---|---|
> | `ConditionAnalyzer` / `CompatibilityVerifier` | `yang_rag/dspy/` | ❌ Static hand-crafted prompts. No optimizer run yet. |
> | `RuleGeneratorModule` | `yang_rag/dspy/modules.py` | ✅ `BootstrapFewShot` optimizer exists in **`yang_rag/pipeline/optimize_prompts.py`** — run manually after collecting 50–100+ training examples from `yang_compatibility_pipeline.py`. |
>
> See [How the Prompt Evolves Over Time](#-how-the-prompt-evolves-over-time) for details.

---

## 📦 Module Structure

```
yang_rag/dspy/
├── __init__.py                    # Public API exports
├── __main__.py                    # python -m yang_rag.dspy entry-point
│
├── llm_verification.py            # ⭐ CORE LLM ANALYSIS MODULE
│   ├── AnalyzeConditionChange     (Signature) — when/must condition diffs
│   ├── VerifyCompatibilityDecision (Signature) — rule-based decision checks
│   ├── ConditionAnalyzer          (Module, ChainOfThought)  ← static prompt, no optimizer yet
│   ├── CompatibilityVerifier      (Module, ChainOfThought)  ← static prompt, no optimizer yet
│   ├── analyze_condition_change() — main entry point for condition analysis
│   ├── verify_compatibility_change()
│   ├── batch_verify_changes()
│   └── process_report_with_deep_analysis_markers()
│
├── llm_verification_hook.py       # Integration layer for the comparator pipeline
│   └── verify_enriched_report()  — processes <needs-deep-analysis> markers,
│                                    writes enriched_report_llm_verified.txt
│                                    and llm_verification_report.json
│
├── signatures.py                  # Rule-generation DSPy signatures (static, hand-crafted)
│   ├── GenerateCompatibilityRule  — single rule generation
│   ├── GenerateAllCompatibilityRules — full rule-set generation
│   ├── ValidateGeneratedRule      — XML schema validation
│   └── ExplainCompatibilityRule   — human-readable explanation
│
├── modules.py                     # RuleGeneratorModule — chains 4 ChainOfThought steps
│   └── RuleGeneratorModule        (generate → validate → explain, all static prompts)
│                                    ⚡ Optimized by BootstrapFewShot via
│                                    yang_rag/pipeline/optimize_prompts.py
│                                    (run manually after 50–100+ training examples)
│
├── pipeline.py                    # generate_rule_for_unknown_keyword():
│   │                                RAG keyword lookup → XML rule extraction →
│   │                                DSPy generation via RuleGeneratorModule
│   │                                NOTE: generation pipeline, NOT an optimizer pipeline.
│   └── RuleGenerationResult       (dataclass with 12 fields)
│
├── xml_utils.py                   # XML extraction utilities
├── api_config.py                  # FuelIX / OpenAI-compatible API setup
│   │                                Prefixes model names with "openai/" to prevent
│   │                                litellm's Anthropic auto-detection (/v1/v1 bug)
├── xml_rule_updater.py            # Helpers to write back to compatibility_rules.xml
├── display.py                     # Rich-formatted output
├── cli.py                         # Command-line interface
└── README.md                      # This file
```

---

## 🎯 Core Capability: Condition Analysis

`AnalyzeConditionChange` is called for every `<needs-deep-analysis>` tag in the enriched
report. It receives the raw `old_condition` and `new_condition` strings and must decide:

| Classification | Meaning | Compatibility |
|---|---|---|
| `NARROWED` | New rejects something old accepted | Non-backward-compatible |
| `RELAXED` | New accepts everything old did, plus more | Backward-compatible |
| `EQUIVALENT` | Identical acceptance sets | Backward-compatible |
| `INCOMPARABLE` | Cannot determine set relationship without full schema | Conservative: keep original decision |

### Quick Usage

```python
from yang_rag.dspy import analyze_condition_change

classification, explanation, confidence = analyze_condition_change(
    old_condition="../../config/type='ACL_IPV4'",
    new_condition="../../../config/type='ACL_IPV4'",
    context={'yang_path': '.../acl-entry/ipv4', 'constraint': 'when', 'action': 'changed'},
)
# → ('incomparable', 'XPath depth changed from 2 to 3 ancestor levels...', 'high')
```

### Current DSPy Signature (`AnalyzeConditionChange`)

The signature's docstring is the system instruction sent to the LLM. Key rules embedded:

```
1. Backward-compatibility ⟺ OLD_VALID_SET ⊆ NEW_VALID_SET
2. XPath depth-shift (../../ → ../../../): navigates to a DIFFERENT ancestor
   → classify INCOMPARABLE unless schema topology confirms same target node
3. Boolean conditions: construct a truth table to verify OLD ⊆ NEW
4. The ConditionAnalyzer.forward() method auto-detects depth changes and
   appends an explicit annotation to the `context` field before the LLM call
```

---

## 🔁 How the Prompt Evolves Over Time

### Current Approach: Manual Refinement

The prompt is a **hand-crafted docstring** on the `AnalyzeConditionChange` signature.
It is updated when systematic failures are observed. For example:

> **Problem** (observed March 2026): 12 identical `../../` → `../../../` `when`-condition
> changes across the same module produced 3 `confirmed` and 9 `unconfirmed` results —
> despite the inputs being semantically identical.
>
> **Root cause**: The signature had no guidance on XPath relative-path depth semantics,
> so the LLM sometimes classified depth-only changes as `relaxed` (incorrect).
>
> **Fix applied**:
> - Added an explicit *XPath depth-shift* section to the signature docstring
> - Added auto-annotation in `ConditionAnalyzer.forward()`: detects when `../` count
>   differs and appends `"NOTE: XPath ancestor depth changed from N to M levels up…"` to context
>
> **Result**: All 12 identical cases now produce `INCOMPARABLE / HIGH confidence` consistently.

The improvement cycle is:
```
Run pipeline → observe inconsistencies → identify missing domain rule
→ add rule to signature docstring or ConditionAnalyzer.forward()
→ re-run → verify consistency
```

### Future Approach: DSPy Automatic Optimization for Condition Analysis (not yet implemented)

> **Note on Rule Generation**: `RuleGeneratorModule` (for generating XML compatibility rules for
> unknown YANG keywords) **already has** a `BootstrapFewShot` optimizer at
> `yang_rag/pipeline/optimize_prompts.py`. That optimizer is run manually after collecting
> 50–100+ training examples via `yang_compatibility_pipeline.py`. See that file for full details.
>
> The section below describes the **not-yet-implemented** optimizer for the **condition analysis**
> path (`ConditionAnalyzer` / `CompatibilityVerifier` in this `yang_rag/dspy/` module).

**To implement automatic optimization**, the steps would be:

```python
import dspy
from yang_rag.dspy.llm_verification import ConditionAnalyzer

# Step 1 — Build a labelled training set
trainset = [
    dspy.Example(
        old_condition="../../config/type='ACL_IPV4'",
        new_condition="../../../config/type='ACL_IPV4'",
        context="yang_path: .../acl-entry/ipv4 | constraint: when | action: changed",
        classification="INCOMPARABLE",
        confidence="HIGH",
    ).with_inputs("old_condition", "new_condition", "context"),

    dspy.Example(
        old_condition="type='A' or type='B'",
        new_condition="type='A' or type='B' or type='C'",
        context="yang_path: .../interface | constraint: when | action: changed",
        classification="RELAXED",
        confidence="HIGH",
    ).with_inputs("old_condition", "new_condition", "context"),

    # ... collect 20–50 labelled examples from real YANG diffs
]

# Step 2 — Define a metric
def classification_match(example, prediction, trace=None):
    return prediction.classification.strip().upper() == example.classification.upper()

# Step 3 — Run the optimizer
optimizer = dspy.MIPROv2(metric=classification_match, num_candidates=10)
optimized = optimizer.compile(
    ConditionAnalyzer(),
    trainset=trainset,
    requires_permission_to_run=False,
)

# Step 4 — Persist the optimized state
optimized.save("yang_rag/dspy/optimized_condition_analyzer.json")

# Step 5 — Load on subsequent runs (in analyze_condition_change())
analyzer = ConditionAnalyzer()
analyzer.load("yang_rag/dspy/optimized_condition_analyzer.json")
```

When implemented, the optimizer would:
- Automatically select the most informative few-shot examples from the training set
- Refine the instruction text to maximise accuracy on the metric
- Persist results so every future run benefits without re-optimizing

> **⚠️ Clarification on `pipeline.py`**: `yang_rag/dspy/pipeline.py` is the **rule generation pipeline**
> (RAG keyword search → XML extraction → DSPy generation via `RuleGeneratorModule`). It is not a
> DSPy optimizer pipeline. The optimizer for `RuleGeneratorModule` lives separately at
> **`yang_rag/pipeline/optimize_prompts.py`** and uses `BootstrapFewShot` — run manually
> after collecting enough training data via `yang_compatibility_pipeline.py`.

**Status for condition analysis**: `optimized_condition_analyzer.json` does not yet exist. `ConditionAnalyzer` and `CompatibilityVerifier` use static hand-crafted signatures.

---

## 🗂️ Output Files Written by `verify_enriched_report()`

| File | Description |
|---|---|
| `output/enriched_report_llm_verified.txt` | Enriched report with `<needs-deep-analysis>` replaced by `<confirmed>` or `<unconfirmed>` |
| `output/llm_verification_report.json` | Machine-readable verification details consumed by `generate_non_compatibility_list.py` to populate `llm_assistance_decision` in `final_report.json` |

`llm_verification_report.json` format:
```json
{
  "items": [
    {
      "change": {
        "yang_path": "openconfig-acl/.../acl-entry/ipv4",
        "keyword_or_constraint": "when",
        "action": "changed",
        "old_value": "../../config/type='ACL_IPV4'",
        "new_value": "../../../config/type='ACL_IPV4'"
      },
      "verification": {
        "verified_compatibility": "confirmed",
        "llm_classification": "incomparable",
        "confidence": "high",
        "explanation": "XPath depth changed from 2 to 3 ancestor levels..."
      }
    }
  ]
}
```

---

## 🔧 Environment Setup

```bash
# FuelIX API (OpenAI-compatible)
export FUELIX_API_KEY="ak-..."

# Or standard OpenAI
export OPENAI_API_KEY="sk-..."
```

`api_config.py` automatically prefixes unqualified model names with `openai/` to force
the OpenAI-compatible code path in litellm, avoiding the doubled `/v1/v1/messages` URL
that occurs when litellm auto-detects `claude-*` as an Anthropic model.

---

## 🧩 Compatibility Verification (`VerifyCompatibilityDecision`)

Used for rules with `assistance="true"` in `compatibility_rules.xml`. The LLM receives
the full context (old value, new value, action, constraint type) and returns:

- `CONFIRMED` — agrees with the rule-based decision
- `OVERRIDDEN` — disagrees; suggests a different compatibility verdict
- `UNCERTAIN` — needs human review

This path is separate from condition analysis and is invoked via `batch_verify_changes()`
through `enrich_compatibility_with_llm()` in the hook.

---

## 📚 DSPy Architecture Reference

| Concept | What it is | Status in this codebase |
|---|---|---|
| **Signature** | Declares inputs, outputs, and docstring instruction | All active — `AnalyzeConditionChange`, `GenerateCompatibilityRule`, etc. |
| **Module** | Wraps a signature with a reasoning strategy | All active — `ConditionAnalyzer(ChainOfThought)`, `RuleGeneratorModule`, etc. |
| **Optimizer** | Automatically improves instructions + few-shots | `BootstrapFewShot` exists for `RuleGeneratorModule` in `yang_rag/pipeline/optimize_prompts.py` — run manually. `MIPROv2` for condition analysis not yet implemented. |
| **Compiled state** | Saved optimized prompts (JSON) | `optimized_condition_analyzer.json` does not exist yet. `RuleGeneratorModule` optimized state saved to `data/optimized_prompts.json` after running the optimizer. |

See the [DSPy documentation](https://dspy.ai) for full optimizer API details.

---

## 🐛 Troubleshooting

### Inconsistent LLM results for identical inputs
Symptom: Same `old_condition` / `new_condition` gives different classifications on different runs.  
Fix: Add an explicit rule to the `AnalyzeConditionChange` docstring covering the problematic pattern, and/or add auto-annotation in `ConditionAnalyzer.forward()`.

### `llm_assistance_decision` always `"none"` in `final_report.json`
Cause: `llm_verification_report.json` was not written, or `generate_non_compatibility_list.py` read `LLM_DECISIONS` at import time before the file existed.  
Fix: Both issues are resolved — `verify_enriched_report()` writes the JSON file, and `main()` in `generate_non_compatibility_list.py` reloads `LLM_DECISIONS` at runtime.

### API `/v1/v1/messages` doubled path
Cause: litellm auto-detects `claude-*` as Anthropic and appends its own `/v1`.  
Fix: `api_config.py` prefixes unqualified model names with `openai/` (e.g. `openai/claude-sonnet-4-6`).

