# Site configuration — files an engineer edits

Clinical protocol is not something a ward changes through a web form at 3 a.m.
Everything in this directory is a plain file, read at server start, reviewed
like code, and versioned like code.

| File | What it configures | Loaded by |
|---|---|---|
| `order_sets.json` | Named order sets / protocols (sepsis, ACS, DKA, post-op, asthma) | `services/order_sets_service.py` |

## Rules that apply to every file here

1. **A malformed file stops the server.** It does not load partially and it
   does not load empty. A protocol that silently disappeared would be worse
   than one that never existed, because the ward would go looking for it.
   Errors name the file, the line, and the exact field.
2. **Editing a file requires a restart.** There is no hot reload, on purpose:
   the set a clinician saw at the top of the shift is the set they applied at
   the end of it.
3. **Nothing here is a safety override.** Every order produced from these files
   still passes the allergy interlock, the dose guard and the
   discharged-patient check, at the moment it is written, exactly as a
   hand-typed order does.

## Pointing Orb at a different file

Set `ORB_ORDER_SETS_FILE` to an absolute path. Used by tests and by sites that
keep their protocols in their own configuration-management repository.
