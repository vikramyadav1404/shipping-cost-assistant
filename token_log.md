# Token / cost log - one full run

- This run used **no LLM** (no API key set): **0 calls, 0 tokens, $0**.

ESTIMATE for LLM mode (not measured - chars/4 rule, ~110 output tokens per note):
- Calls: 10 (one per context note; repeat runs hit the cache = 0 calls)
- Input tokens: ~3064, output tokens: ~1100
- Cost on llama-3.1-8b-instant at $0.05/M in, $0.08/M out: ~$0.00024 (free tier: $0)

Run with `LLM_API_KEY` set to replace these estimates with real numbers.

The weekly numbers, comparisons and flags never call an LLM (pure pandas), so cost does
not grow with the number of shipments - only with the number of notes.
