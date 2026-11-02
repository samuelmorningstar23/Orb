# Retrieval executor selection

The application's `SageRetrievalExecutor.from_env()` and example hospital
profile now select **four retrieval threads**. An explicit
`ORB_SAGE_RETRIEVAL_WORKERS` value from 1 through 8 overrides this setting;
invalid values fail startup. The generic `SageRetrievalExecutor()` constructor
retains its conservative one-thread default.

This revises the initial chart-only choice. In the controlled full-RAG
comparison, both widths completed 200 of 200 HTTP requests with the installed
BGE-M3 embedder, FP16 BGE-reranker-v2-m3, the same corpus, unchanged application
source and a mocked 200-slot generation engine. Width 1 took **27.943 seconds**;
width 4 took **26.821 seconds**, about 4% sooner. Full-answer target hits also
rose from 6 to 7 general queries, 9 to 12 patient queries, and 36 to 38 department
queries. Both runs performed 134 embedding calls and 134 cross-encoder calls,
returning 1,608 cross-encoder results without a model failure or lexical downgrade.

**Neither width met the 200-request 3/5/15-second targets.** Four is the better
measured starting point for the actual retrieval path, not a universal hardware
optimum or a generation-capacity claim. The isolated knowledge worker still
serializes embedding/reranking; synchronous RPC waits can hold a retrieval
thread and delay other requests. Removing guidelines, lowering relevance
thresholds or substituting lexical-only retrieval would change this comparison
and is not part of the optimisation.

The implementation bounds active plus queued jobs at 200. Only a configured
thread's worth of jobs enter `ThreadPoolExecutor`; the remaining queue is owned
by the application. Cancellation removes queued jobs and their inputs. Running
or dispatched physical work retains admission until it finishes. ContextVars
are copied separately for each call. Per-application state can span request
event loops, and closing an application cannot create a second admission budget
while its old threads are still finishing.

Shutdown closes retrieval admission first, stops the knowledge child, then joins
retrieval threads. A real-app lifecycle regression verifies this order with a
synthetic blocked RPC: it would fail if joining happened before releasing the
RPC. Metrics expose counts and coarse timings; they contain no clinical input,
patient identifier, result or callable name.

The exact-string name/bed preprocessing caches are bounded, volatile process
memory and contain PHI when names are parsed. They never store authorisation or
patient-selection decisions; live database scope and source reads remain in
place. These caches are not logged or persisted. Existing erasure handling has
no process-cache purge hook; a process restart clears their retained strings.

Evidence: `round4-full-rag-width1-200-mock.json`,
`round4-full-rag-width4-200-mock.json` and `profiling-notes.md` in this directory.
These mocked-generation reports must remain separate from real-model serving
results. Repeat qualification after changing hardware, model or workload.
