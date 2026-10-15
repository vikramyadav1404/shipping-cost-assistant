# Eval report

**Score: 18/18 passed**

| part | case | result | detail |
|---|---|---|---|
| A labelled | Delhi-Jaipur 2024-11-11 -> Yes | PASS | got ('Yes', '') / no note covers Delhi-Jaipur in Nov 2024 (N007 is May 2024 and says roads improved) |
| A labelled | Delhi-Jaipur 2024-11-18 -> Yes | PASS | got ('Yes', '') / same spike, second week, still no note |
| A labelled | Ahmedabad-Mumbai 2025-01-20 -> No (justified) N002 | PASS | got ('No (justified)', 'N002') / festival-week surcharge on this corridor, that exact week |
| A labelled | Chennai-Bangalore 2025-02-24 -> No (justified) N001 | PASS | got ('No (justified)', 'N001') / flooding Feb 24 - Mar 8 |
| A labelled | Chennai-Bangalore 2025-03-03 -> No (justified) N001 | PASS | got ('No (justified)', 'N001') / flooding Feb 24 - Mar 8 |
| A labelled | Chennai-Bangalore 2025-03-10 -> Yes | PASS | got ('Yes', '') / flood window ended Mar 8; N009 says the route is back to normal |
| A labelled | Chennai-Bangalore 2025-03-17 -> Yes | PASS | got ('Yes', '') / still high after the flood window |
| A labelled | Mumbai-Pune 2025-09-15 -> Yes | PASS | got ('Yes', '') / N006 only says demand was stable |
| A labelled | Mumbai-Delhi 2025-05-12 not flagged | PASS | got None / diesel week (+11.6%): an ordinary 5-7% nationwide rise, not an anomaly |
| A labelled | sample numbers Delhi-Jaipur 2024-11-11 | PASS | ours ('4.17', '+35.5%', '+21.0%') sample ('4.17', '+35.5%', '+21.0%') |
| A labelled | sample numbers Ahmedabad-Mumbai 2025-01-20 | PASS | ours ('3.29', '+29.5%', '+22.5%') sample ('3.29', '+29.5%', '+22.5%') |
| A labelled | sample numbers Mumbai-Pune 2025-09-15 | PASS | ours ('3.98', '+9.2%', '+23.6%') sample ('3.98', '+9.2%', '+23.6%') |
| B integrity | all 19 rows: citations exist, fit the route, named in reason | PASS | ok |
| C guardrail | wrong route: N002 moved to Mumbai-Pune | PASS | Ahmedabad-Mumbai 2025-01-20: expected ('Yes', ''), got ('Yes', '') |
| C guardrail | wrong window: N002 dated 6 weeks later | PASS | Ahmedabad-Mumbai 2025-01-20: expected ('Yes', ''), got ('Yes', '') |
| C guardrail | note says costs NOT affected | PASS | Ahmedabad-Mumbai 2025-01-20: expected ('Yes', ''), got ('Yes', '') |
| C guardrail | stated rise too small (2% vs +35.5%) | PASS | Delhi-Jaipur 2024-11-11: expected ('Yes', ''), got ('Yes', '') |
| C guardrail | positive control: a genuine note is accepted | PASS | Delhi-Jaipur 2024-11-18: expected ('No (justified)', 'N901'), got ('No (justified)', 'N901') |
