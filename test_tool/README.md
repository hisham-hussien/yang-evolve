# YANG Compatibility Checker — Demo Test Suite

This folder contains a curated set of YANG modules that demonstrate the capabilities of the **YANG-RAG Compatibility Checker** tool. Each scenario is designed to showcase a specific class of change and the verdict the tool produces.

---

## Folder Structure

```
test_tool/
├── test_yang_models/          # Old (baseline) versions of all modules
│   ├── vpn-services-old.yang  # Main module — VPN services (old)
│   ├── ep-extensions.yang     # Extension: ep:endpoint (REST URI annotation)
│   ├── pr-extensions.yang     # Extension: pr:privileges (HTTP operations)
│   ├── smiv2-defs.yang        # Extension: SMIv2 annotations (defval, max-access, oid)
│   ├── old-types.yang         # Submodule: type & constraint change test cases
│   ├── old-path.yang          # Submodule: XPath / leafref / when / must test cases
│   └── demo-defval.yang       # Submodule: SMIv2 extension value changes
│
├── test_yang_models_v2/       # New (revised) versions of all modules
│   ├── vpn-services-new.yang  # Main module — VPN services (new)
│   ├── ep-extensions.yang     # (unchanged)
│   ├── pr-extensions.yang     # (unchanged)
│   ├── smiv2-defs.yang        # (unchanged)
│   ├── old-types.yang         # Updated type & constraint values
│   ├── old-path.yang          # Updated XPath / leafref / when / must values
│   └── demo-defval.yang       # Updated SMIv2 extension values
│
└── results/
    ├── Default/               # Comparison output — default compatibility mode
    │   ├── final_report.json
    │   └── concise_final_report.json
    └── RFC/                   # Comparison output — RFC 7950 strict mode
        ├── final_report.json
        └── concise_final_report.json
```

---

## Scenario 1 — Extension Changes: `ep:endpoint` & `pr:privileges`

**Files:** [`vpn-services-old.yang`](test_yang_models/vpn-services-old.yang) → [`vpn-services-new.yang`](test_yang_models_v2/vpn-services-new.yang)

This scenario uses the `ep:endpoint` and `pr:privileges` YANG extensions introduced in:

> *"Model-Driven REST API Generation from YANG Data Models"*  
> Springer SoSyM (2025) — <https://link.springer.com/article/10.1007/s10270-025-01311-3>

The extensions annotate YANG nodes with REST API metadata. The tool detects changes to extension values and classifies them using the compatibility rules.

### `ep:endpoint` changes — REST URI versioning prefix added

All endpoint paths gain the `/api/v1` versioning prefix:

| YANG Node | Old `ep:endpoint` | New `ep:endpoint` | Verdict |
|-----------|-------------------|-------------------|---------|
| `container vpn-services` | `/vpn_services` | `/api/v1/vpn_services` | **NBC** |
| `list vpn-service` | `/vpn_services/{vpn_service_vpn_id}` | `/api/v1/vpn_services/{vpn_service_vpn_id}` | **NBC** |
| `container vpn-nodes` | `/vpn_services/{…}/vpn-nodes` | `/api/v1/vpn_services/{…}/vpn-nodes` | **NBC** |
| `list vpn-node` | `/vpn_services/{…}/vpn-nodes/{…}` | `/api/v1/vpn_services/{…}/vpn-nodes/{…}` | **NBC** |

### `pr:privileges` changes — HTTP operation permissions modified

| YANG Node | Old `pr:privileges` | New `pr:privileges` | Verdict |
|-----------|---------------------|---------------------|---------|
| `container vpn-services` | `"create"` | `"create delete"` | **BC** (privilege added) |
| `container vpn-nodes` | `"create update"` | `"create"` | **NBC** (privilege removed) |

### Summary (Default mode = RFC mode for extensions)

| Category | Count |
|----------|-------|
| Compatible (BC) | 3 |
| Non-Compatible (NBC) | 4 |
| Unmarked | 0 |

> **Note:** Custom extensions (`ep:endpoint`, `pr:privileges`) produce the **same verdict** in both Default and RFC 7950 modes because they are not standard YANG types — the tool applies its extension-specific rules in both cases.

---

## Scenario 2 — Type & Constraint Changes

**Files:** [`old-types.yang`](test_yang_models/old-types.yang) → [`old-types.yang`](test_yang_models_v2/old-types.yang)

This submodule covers the full spectrum of YANG type and constraint changes. It is compared as part of the `vpn-services` main module.

### Type changes

| Leaf | Old type | New type | Default verdict | RFC 7950 verdict |
|------|----------|----------|-----------------|------------------|
| `int8-to-int16` | `int8` | `int16` | **BC** (superset) | **NBC** (type identity changed) |
| `int32-to-int8` | `int32` | `int8` | **NBC** (narrowed) | **NBC** |
| `int8-to-string` | `int8` | `string` | **BC** (string accepts all int8 values) | **NBC** (type identity changed) |
| `leafref-to-int` | `leafref` | `int32` | **NBC** | **NBC** |
| `leafref-to-string` | `leafref` | `string` | **BC** | **NBC** |
| `string-to-leafref` | `string` | `leafref` | **NBC** | **NBC** |

### Constraint changes

| Leaf | Old constraint | New constraint | Verdict |
|------|----------------|----------------|---------|
| `range-relaxed` | `range "0..100"` | `range "0..200"` | **BC** (relaxed) |
| `range-narrowed` | `range "0..200"` | `range "0..100"` | **NBC** (narrowed) |
| `length-relaxed` | `length "1..10"` | `length "1..20"` | **BC** (relaxed) |
| `length-narrowed` | `length "1..20"` | `length "1..10"` | **NBC** (narrowed) |
| `pattern-relaxed` | `pattern "[0-9]+"` | `pattern "[0-9a-zA-Z]+"` | **BC** (relaxed) |
| `pattern-narrowed` | `pattern "[0-9a-zA-Z]+"` | `pattern "[0-9]+"` | **NBC** (narrowed) |

---

## Scenario 3 — XPath / Leafref / `when` / `must` Changes

**Files:** [`old-path.yang`](test_yang_models/old-path.yang) → [`old-path.yang`](test_yang_models_v2/old-path.yang)

This submodule covers XPath-based changes including `leafref` path changes, `when` condition changes, and `must` constraint changes.

### Leafref path changes

| Leaf | Old path | New path | Verdict |
|------|----------|----------|---------|
| `case-a-unchanged` | `../../config/name` | `../../config/name` | **BC** (equivalent) |
| `case-a-rewritten` | `../../config/name` | `/op:config/op:name` | **BC** (same target, absolute form) |
| `case-b-different-target` | `../../config/name` | `../../config/index` | **NBC** (different node) |
| `case-b-different-container` | `../../config/name` | `../../state/name` | **NBC** (different container) |
| `case-c-fix-broken-path` | `../../nonexistent/name` | `../../config/name` | **BC** (broken → valid) |
| `case-d-break-valid-path` | `../../config/name` | `../../nonexistent/name` | **NBC** (valid → broken) |

### `when` condition changes

| Leaf | Old `when` | New `when` | Verdict |
|------|------------|------------|---------|
| `when-narrowed` | `"../enabled = 'true'"` | `"../enabled = 'true' and ../mode = 'active'"` | **NBC** (AND added — fewer nodes match) |
| `when-relaxed` | `"../enabled = 'true' and ../mode = 'active'"` | `"../enabled = 'true'"` | **BC** (condition relaxed) |
| `when-removed` | `"../enabled = 'true'"` | *(removed)* | **BC** (always present now) |
| `when-added` | *(none)* | `"../enabled = 'true'"` | **NBC** (node may disappear) |

### `must` constraint changes

| Leaf | Old `must` | New `must` | Verdict |
|------|------------|------------|---------|
| `must-added` | *(none)* | `". >= 1024 and . <= 65535"` | **NBC** (new restriction) |
| `must-removed` | `". >= 1024 and . <= 65535"` | *(removed)* | **BC** (restriction lifted) |

---

## Scenario 4 — SMIv2 & Custom Extension Value Changes (Unmarked)

**Files:** [`demo-defval.yang`](test_yang_models/demo-defval.yang) → [`demo-defval.yang`](test_yang_models_v2/demo-defval.yang)
**Also:** [`vpn-services-old.yang`](test_yang_models/vpn-services-old.yang) → [`vpn-services-new.yang`](test_yang_models_v2/vpn-services-new.yang)

This scenario covers extension keywords whose compatibility rules are not yet defined in the XML rules file. Changes to these extension values cannot be classified by the standard compatibility rules and appear as **`[UNMARKED]`** in the report.

### SMIv2 extension changes (`smiv2-defs.yang`)

Uses the `smiv2-defs.yang` extension module (defines `smiv2:defval`, `smiv2:max-access`, `smiv2:oid`, `smiv2:alias`).

| Leaf | Extension | Old value | New value | Verdict |
|------|-----------|-----------|-----------|---------|
| `ifMtu` | `smiv2:defval` | `"1500"` | `"9000"` | **UNMARKED** |
| `ifMtu` | `smiv2:max-access` | `"read-write"` | `"read-only"` | **UNMARKED** |

### Custom extension changes (`ep-extensions.yang`, `pr-extensions.yang`)

When the `test_tool/compatibility_rules.xml` file does **not** contain rules for `ep:endpoint` or `pr:privileges`, those changes also appear as `[UNMARKED]`. The table below shows all four extension attributes that can be unmarked depending on the rules file used:

| YANG Node | Extension | Old value | New value | Verdict (no rules) |
|-----------|-----------|-----------|-----------|---------------------|
| `container vpn-services` | `ep:endpoint` | `"/vpn_services"` | `"/api/v1/vpn_services"` | **UNMARKED** |
| `container vpn-services` | `pr:privileges` | `"create"` | `"delete create"` | **UNMARKED** |
| `container vpn-nodes` | `ep:endpoint` | `"/vpn_services/{…}/vpn-nodes"` | `"/api/v1/vpn_services/{…}/vpn-nodes"` | **UNMARKED** |
| `container vpn-nodes` | `pr:privileges` | `"create update"` | `"create"` | **UNMARKED** |

> **`[UNMARKED]`** means the tool detected a change but has no rule to classify it. These entries are candidates for RAG-assisted rule generation (Phase 2) or manual review.

---

## How to Run

### Prerequisites

Build and install the package from the project root:

```bash
python -m build
pip install dist/yang_comparator_pro-1.0.0-py3-none-any.whl
```

Verify the pyang plugin is available:

```bash
pyang --check-compatibility --help
```

### Default compatibility mode (with LLM verification)

```bash
pyang --check-compatibility \
  --old-version vpn-services-old.yang \
  --new-version vpn-services-new.yang \
  --old-dir test_tool/test_yang_models \
  --new-dir test_tool/test_yang_models_v2 \
  --output-dir test_tool/results/Default \
  --llm-verify \
  --llm-model claude-sonnet-4-6 \
  --xml-rules test_tool/compatibility_rules.xml
```

### RFC 7950 strict mode (with LLM verification)

```bash
pyang --check-compatibility \
  --old-version vpn-services-old.yang \
  --new-version vpn-services-new.yang \
  --old-dir test_tool/test_yang_models \
  --new-dir test_tool/test_yang_models_v2 \
  --output-dir test_tool/results/RFC \
  --rfc7950 \
  --llm-verify \
  --llm-model claude-sonnet-4-6 \
  --xml-rules test_tool/compatibility_rules.xml
```

> **Note:** `--xml-rules test_tool/compatibility_rules.xml` points to the local rules file bundled with the test suite. Omit this flag to use the default installed rules.

---

## Default vs RFC 7950 Mode — Side-by-Side

The two modes differ only for **standard YANG type changes**. Custom extension changes (`ep:endpoint`, `pr:privileges`, `smiv2:*`) are classified identically in both modes.

| Change | Default mode | RFC 7950 mode |
|--------|-------------|---------------|
| `int8` → `int16` | ✅ BC (value space is a superset) | ❌ NBC (type identity changed) |
| `int8` → `string` | ✅ BC (string accepts all int8 values) | ❌ NBC (type identity changed) |
| `leafref` → `string` | ✅ BC | ❌ NBC |
| `range "0..100"` → `"0..200"` | ✅ BC (relaxed) | ✅ BC (relaxed) |
| `range "0..200"` → `"0..50"` | ❌ NBC (narrowed) | ❌ NBC (narrowed) |
| `ep:endpoint` path changed | ❌ NBC | ❌ NBC |
| `pr:privileges` privilege added | ✅ BC | ✅ BC |
| `pr:privileges` privilege removed | ❌ NBC | ❌ NBC |
| `smiv2:defval` value changed | ⚠️ UNMARKED | ⚠️ UNMARKED |

> **Key insight:** RFC 7950 mode is stricter about type identity — any change to the base type name is NBC, even if the new type's value space is a superset of the old one. Default mode uses semantic value-space analysis.

---

## Output Files

Each run produces the following files in the output directory:

| File | Description |
|------|-------------|
| `final_report.json` | Full grouped compatibility report (JSON) |
| `concise_final_report.json` | Condensed report (deduped, renames detected) |
| `report.txt` | Raw diff report (intermediate) |
| `enriched_report_llm.txt` | Report enriched with compatibility tags |
| `compatible_list.txt` | List of BC changes |
| `non_compatible_list.txt` | List of NBC changes |
