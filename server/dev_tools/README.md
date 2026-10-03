# dev_tools

Ad-hoc developer/debugging scripts. **Not** part of the running server and not
imported by application code — they were previously scattered in the `server/`
root, where they cluttered the package and risked being mistaken for runtime
modules. Moved here to keep the server package clean.

Several of these import sibling modules (`database`, `config`, `services`, ...),
so run them from the `server/` directory with that directory on the path:

```bash
cd server
PYTHONPATH=. python dev_tools/get_room.py
```

| Script | Purpose |
|---|---|
| `get_pat.py` | Fetch/inspect a patient row from the DB |
| `get_room.py` | Fetch/inspect a Relay case room |
| `add_test_action.py` | Insert a test ambient action card |
| `dump_html.py` | Playwright HTML dump of a page (needs `playwright`, see `requirements-dev.txt`) |
| `time_llm_queries.py` | Ad-hoc timing of LLM queries |
| `time_pipeline.py` | Ad-hoc timing of a processing pipeline |
| `time_verify.py` | Ad-hoc timing of the verification stage |
| `check_fast_model_contract.py` | Does a model emit Sage's leading `NOACT|`/`ACT|`, and does the real gate stream it? |
| `measure_thinking_mode.py` | Thinking ON vs OFF, through the real gate: time to first readable word |
| `check_history_marker_replay.py` | Does a markerless turn in the history cost the next turn its marker? |
| `check_action_lane_gates.py` | Which Sage turns get a deterministic gate: `detect_mutation_intent` x `classify_floor` over the corpus in `tests/action_lane_corpus.py`. Offline, exits non-zero on drift |
