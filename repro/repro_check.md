# Reproducibility check

3 independent runs (separate processes, LLM cache disabled so any LLM would be re-called each time) on the same input.

| run | rows | sha256 |
|---|---|---|
| run1.csv | 19 | `773ce3d48894d408...` |
| run2.csv | 19 | `773ce3d48894d408...` |
| run3.csv | 19 | `773ce3d48894d408...` |

**Result: IDENTICAL - all runs byte-for-byte the same**

diff run1.csv vs run2.csv: (empty)
diff run1.csv vs run3.csv: (empty)
