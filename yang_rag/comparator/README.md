# YANG Comparator

Pyang-based YANG module comparison engine with semantic XPath resolution.

## Overview

This comparator:
- Parses YANG modules using pyang
- Compares old vs new versions
- Applies compatibility rules from `compatibility_rules.xml`
- **Resolves XPath expressions** in `when`/`must` statements to detect semantic changes
- Generates enriched reports with compatibility tags

## Key Features

### ✨ XPath Semantic Resolution (NEW!)

When comparing YANG `when` and `must` statements, the comparator now resolves relative XPath expressions to absolute schema paths. This enables detection of **breaking changes** where the XPath syntax changes but the **semantic meaning** changes.

**Example**: 
```yang
# OLD: when "../../../config/type = 'TACACS'"
# NEW: when "../../config/type = 'TACACS'"
```

Simple string comparison would miss this, but XPath resolution detects:
- **Old** resolves to: `/module/server-group/config/type`
- **New** resolves to: `/module/servers/config/type`
- **Result**: ❌ **BREAKING CHANGE** (different nodes!)

See [XPATH_RESOLUTION.md](XPATH_RESOLUTION.md) for detailed documentation.

## Usage

Used internally by the main pipeline. See main [README.md](../README.md) for usage.

## Direct Usage

```bash
./yang_comparator.sh example-old.yang example-new.yang /path/to/modules1 /path/to/modules2
```

Output:
- `output/report.txt` - Basic comparison
- `output/enriched_report.txt` - With compatibility tags
- `output/compatible_list.txt` - Compatible changes
- `output/non_compatible_list.txt` - Breaking changes
- `output/final_report.json` - JSON format
