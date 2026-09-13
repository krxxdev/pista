# eSIM commit-candidate secret audit

Date: 2026-09-13. Scanner: `system/hardware/tici/tools/scan_esim_candidate.py` under Python 3.14.5, fully offline with `GIT_NO_LAZY_FETCH=1`.

## Scope and result

The private `telekom-esim.txt` source was excluded from candidate material. The scanner read qualifying credential values in memory only to build direct and URL-encoded exact-match rules. It emitted no matched value, raw line, context, hash, argv value, or environment value.

| scope | result |
|---|---|
| Sparse working tree | Zero blocking direct, URL-decoded, live activation-shape, or URL-encoded activation-shape findings. Decimal identifier heuristic: 8 occurrences in 3 files, reviewed as synthetic test/fixture identifiers. |
| Git index | Zero blocking findings among locally available objects. Decimal identifier heuristic: 3 occurrences in 2 files. The index itself was unchanged by this preparation. |
| Intended ancestry (`HEAD`) | Incomplete offline audit: the partial clone could not enumerate ancestry objects with lazy fetch disabled. |
| Explicit candidate export | Zero blocking findings. All identifier-like values are the documented synthetic `8900…` fixtures or masked suffix examples; no real subscriber identifier is included. |
| Generated review patch | Zero blocking findings after generation from an isolated temporary index. Eight identifier-like occurrences are the same reviewed synthetic fixture values. |

## Partial-clone limitation

The local object store lacks 1043 index blobs, and `git rev-list --objects HEAD` cannot complete without unavailable objects. No lazy fetch or network request was attempted. Therefore this preparation cannot make a complete secret claim about every historical blob in the intended ancestry.

Before publication, repeat the same value-suppressing scan in an authorized complete local clone or produce a clean-base export containing only the explicit manifest. If a private value is found in old commits, classify publication as blocked and prepare a clean-base export; do not rewrite pushed history automatically.

## Rule coverage and limits

Covered: direct qualifying private values, percent/URL-encoded and URL-decoded copies, recognizable non-synthetic `LPA:` forms, URL-encoded activation prefixes, and decimal 18–32 digit identifier heuristics. Reserved `.invalid` and `example.com/.net/.org` fixtures are treated as synthetic.

This is not an absolute guarantee against arbitrary encryption, compression, steganography, novel encodings, unavailable Git objects, or files outside the scanned repo/export. No raw conversation export, workspace bundle, Git bundle, release archive, or private journal is in the manifest.
