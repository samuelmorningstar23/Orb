"""TEMPORARY DIAGNOSTIC — its question has been answered; safe to delete.

Runs last (filename sorts after test_patient_scope_routes.py) and re-does that
file's /{patient_id} GET enumeration, dumping identity for anything it finds
unguarded so the order-dependent failure can be explained.

THE ANSWER (2026-08-15). It was never order dependence and never a route.
routes/emar_routes.py was saved while a full-suite run was in flight:
get_patient_administrations moved from line 116 to line 197, but the imported
code object still carried co_firstlineno=116. inspect.getsource() re-reads the
file at call time while trusting that stale number, so findsource() walked
backwards from line 116 and returned `administer_medication` — whose body has no
ward check. Duration is the whole reason it only showed up in a full run: a
40-minute suite leaves a wide window between import and assertion, a 20-second
single-file run leaves none. The dump below now names that case explicitly
instead of filing it under UNGUARDED.
"""
import inspect


def _source_really_belongs_to(endpoint, src: str) -> bool:
    """Mirror of the identity check in test_patient_scope_routes.py."""
    name = getattr(endpoint, "__name__", None)
    if not name:
        return True
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("def ", "async def ")):
            return stripped.split("def ", 1)[1].split("(")[0].strip() == name
    return True


def test_zzz_diagnose_patient_id_guard_enumeration():
    from main import app as live_app

    report, drifted = [], []
    for route in live_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "{patient_id}" not in path or "GET" not in methods:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            src = inspect.getsource(endpoint)
            err = None
        except Exception as exc:  # noqa: BLE001
            src, err = "", repr(exc)
        if "require_patient_access" in src or "_authorize_patient_access" in src:
            continue
        code = getattr(endpoint, "__code__", None)
        entry = (
            f"PATH={path}\n"
            f"  endpoint={getattr(endpoint, '__module__', '?')}."
            f"{getattr(endpoint, '__qualname__', '?')} id={id(endpoint)}\n"
            f"  wrapped={getattr(endpoint, '__wrapped__', None)!r}\n"
            f"  co_filename={getattr(code, 'co_filename', '?')}\n"
            f"  co_firstlineno={getattr(code, 'co_firstlineno', '?')}\n"
            f"  getsource_error={err}\n"
            f"  src_head={src.splitlines()[:6]}\n"
        )
        (drifted if src and not _source_really_belongs_to(endpoint, src) else report).append(entry)

    assert not drifted, (
        "STALE SOURCE, NOT AN UNGUARDED ROUTE — getsource() returned a different "
        "function's body than the one on the route, because the file was edited "
        "after it was imported. Re-run against a quiescent checkout:\n"
        + "\n".join(drifted)
    )
    assert not report, "UNGUARDED (diagnostic dump):\n" + "\n".join(report)
