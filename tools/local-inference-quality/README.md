# Local inference quality gate

This independent gate adds strict typing, lint/format checks and 100% statement
and branch coverage for the loopback-origin validator, private ASR stdin protocol
bounded inference cache, security-header middleware, SPA bootstrap and warm-model policy. It supplements the existing importer/release gate;
it does not claim whole-backend compliance.

From a separate Python 3.12 developer environment:

```sh
python -m pip install -r tools/local-inference-quality/requirements.txt
bash tools/local-inference-quality/check.sh
```

The coverage selection uses `--noconftest` and only pure tests. It never starts
Orb, initializes a database, accesses model weights or opens network connections.
The separate application tests cover speech subprocess wiring, authenticated
sockets, Lens persistence/cleanup and cache input/model invalidation. The CI job
runs this gate independently from the application runtime and uploads coverage.
