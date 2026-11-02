#!/usr/bin/env python3
"""Reproduce a static repository inventory without importing application code.

Reads only allowlisted source/config/documentation text from Git's tracked and
nonignored untracked listing. Never opens databases, environment files, media,
runtime data, backups, dependency trees, or symlinks. All findings are static
candidates; this does not prove runtime registration, authorization, or dead code.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".py", ".sql", ".md", ".mdx", ".json", ".toml", ".yaml", ".yml",
    ".sh", ".css", ".html", ".txt", ".ini", ".cfg", ".conf",
}
TEXT_NAMES = {"Dockerfile", "Makefile", ".gitignore", ".dockerignore", ".nvmrc"}
EXCLUDED_PARTS = {
    ".git", "node_modules", ".venv", ".venv-dev", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "dist", "build",
    "coverage", "playwright-report", "test-results", "uploads", "media",
    "data", "backups", "backup", "logs", "artifacts", ".codex", ".agents",
    ".aws", "screenshots",
}
SECRET_NAME = re.compile(r"(?:^\.env(?:\.|$)|(?:secret|credential|private[-_]?key)|\.(?:pem|key|p12|pfx|db|sqlite\d*|bak|backup|log)$)", re.I)
MARKERS = re.compile(r"\b(TODO|FIXME|HACK|STUB|NotImplementedError|NotImplemented)\b")
CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".sql"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "websocket", "api_route"}
REVIEWED_MARKERS = {
    ("server/routes/vigil_routes.py", 643): "Optimization: the baseline is implemented; precomputed risk reuse would avoid repeated model inference per chart open.",
    ("server/services/bot_service.py", 369): "Deferred retention policy: current row pruning is restricted to simulated vitals; real ingested history is retained.",
    ("server/scripts/train_vigil_cfc.py", 55): "Deferred training/serving skew correction; model readiness work, not an empty application route.",
    ("server/scripts/train_vigil_cfc.py", 214): "Explicitly unsupported non-sepsis training adapters until credentialed, timestamp-labelled ICU datasets are available; raises rather than silently mocking results.",
    ("server/scripts/train_vigil_cfc.py", 353): "Deferred checkpoint promotion quality gate; requires validation evidence before additional model activation.",
    ("src/hooks/useStreamingTranscription.ts", 135): "Deferred ScriptProcessor-to-AudioWorklet migration: implemented dictation path has main-thread/deprecated-API technical debt.",
}


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def exclusion(path: str, full: Path) -> str | None:
    parts = PurePosixPath(path).parts
    if any(p in EXCLUDED_PARTS and not (p == "build" and path.startswith("docs/his_design/build/")) for p in parts):
        return "dependency/generated/runtime directory"
    if path.startswith("docs/audit-2026-10/") or path == "scripts/audit_repository.py":
        return "audit output/generator (avoid self-inventory)"
    if full.name == ".npmrc" or SECRET_NAME.search(full.name):
        return "secret/environment/database/backup filename"
    if full.is_symlink():
        return "symlink (target not followed)"
    if not full.is_file():
        return "deleted/non-file path"
    if full.suffix.lower() not in TEXT_SUFFIXES and full.name not in TEXT_NAMES:
        return "non-allowlisted/binary file type"
    if full.stat().st_size > 2_000_000:
        return "text above 2 MB inspection bound"
    return None


def is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return (any(part in {"tests", "test", "e2e", "__tests__", "fixtures"} for part in p.parts)
            or p.name.startswith("test_") or bool(re.search(r"\.(test|spec)\.", p.name)))


def production(path: str) -> bool:
    return path.startswith(("src/", "server/")) and not is_test(path) and PurePosixPath(path).suffix in CODE_SUFFIXES


def category(path: str) -> str:
    if is_test(path):
        return "tests/fixtures"
    if path.startswith("src/"):
        return "frontend"
    if path.startswith("server/"):
        return "backend"
    if path.startswith("docs/") or PurePosixPath(path).suffix in {".md", ".mdx"}:
        return "documentation"
    if path.startswith(".github/"):
        return "github/CI"
    if path.startswith("scripts/"):
        return "scripts"
    return "root/config/other"


def marker_inventory(texts: dict[str, str]) -> dict:
    all_markers, production_markers = [], []
    for path, text in texts.items():
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            found = sorted(set(MARKERS.findall(line)))
            if not found:
                continue
            context = " ".join(lines[max(0, number - 3):number + 3]).lower()
            if re.search(r"(?:defer|abstract|subclass|override|not supported|intentionally|optional|feature.flag|flag.off|held|gated|future|roadmap)", context):
                disposition = "deliberate/deferred-or-contract candidate; inspect context"
            elif is_test(path):
                disposition = "test/fixture; not a production implementation gap"
            elif not production(path):
                disposition = "documentation/config/script reference; not automatically an implementation gap"
            else:
                disposition = "review candidate; marker alone does not prove a defect"
            entry = {"path": path, "line": number, "markers": found, "text": line.strip()[:400], "disposition": disposition}
            if (path, number) in REVIEWED_MARKERS:
                entry["reviewed_context"] = REVIEWED_MARKERS[path, number]
            all_markers.append(entry)
            if production(path):
                production_markers.append(entry)
    return {"production": production_markers, "all_inspected_text": all_markers,
            "production_marker_counts": dict(Counter(m for e in production_markers for m in e["markers"])),
            "interpretation": "Occurrences include comments, strings, abstract contracts, imports, and exception guards. No marker is automatically an unfinished feature."}


def ts_tokens(text: str) -> list[tuple[str, str, int]]:
    """Small lexical scanner: preserve literal imports; skip comments safely."""
    out, i, line = [], 0, 1
    while i < len(text):
        c = text[i]
        if c.isspace():
            line += c == "\n"
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            line += text[i:end].count("\n")
            i = end
        elif c in "\"'`":
            start, start_line, quote = i, line, c
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    line += text[i] == "\n"
                    i += 1
            out.append(("template" if quote == "`" else "string", text[start + 1:i - 1], start_line))
        elif c.isalpha() or c in "_$":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in "_$"):
                i += 1
            out.append(("identifier", text[start:i], line))
        else:
            out.append(("punctuation", c, line))
            i += 1
    return out


def ts_graph(texts: dict[str, str], root: Path) -> dict:
    modules = {p for p in texts if p.startswith("src/") and PurePosixPath(p).suffix in {".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"} and not is_test(p) and not p.endswith(".d.ts")}
    edges, unresolved = [], []
    adjacency = defaultdict(set)

    def resolve(path: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return None
        base = (root / PurePosixPath(path).parent / spec).resolve()
        try:
            relative = base.relative_to(root).as_posix()
        except ValueError:
            return None
        candidates = [relative] + [relative + ext for ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")]
        candidates += [relative + "/index" + ext for ext in (".ts", ".tsx", ".js", ".jsx")]
        if relative.endswith(".js"):
            candidates += [relative[:-3] + ".ts", relative[:-3] + ".tsx"]
        return next((p for p in candidates if p in modules), None)

    for path in sorted(modules):
        tokens = ts_tokens(texts[path])
        for index, (_, token, line) in enumerate(tokens):
            if token not in {"import", "export"}:
                continue
            rest = tokens[index + 1:index + 150]
            specs, kind = [], "literal import/export (includes type references)"
            if [t[1] for t in rest[:5]] == [".", "meta", ".", "glob", "("]:
                kind = "import.meta.glob"
                glob_tokens = rest[5:]
                if glob_tokens and glob_tokens[0][1] == "[":
                    end = next((i for i, t in enumerate(glob_tokens) if t[1] == "]"), len(glob_tokens))
                    specs = [t[1] for t in glob_tokens[:end] if t[0] == "string"]
                elif glob_tokens and glob_tokens[0][0] == "string":
                    specs = [glob_tokens[0][1]]
                selected = set()
                for spec in specs:
                    negative = spec.startswith("!")
                    pattern = spec[1:] if negative else spec
                    matched: set[str] = set()
                    try:
                        matched = {p.relative_to(root).as_posix() for p in (root / Path(path).parent).glob(pattern)} & modules
                    except (ValueError, NotImplementedError):
                        unresolved.append({"path": path, "line": line, "specifier": spec, "kind": kind})
                    if negative:
                        selected -= matched
                    else:
                        selected |= matched
                for target in sorted(selected):
                    adjacency[path].add(target)
                    edges.append({"from": path, "to": target, "line": line, "kind": kind})
                continue
            if token == "import" and rest and rest[0][0] == "string":
                specs = [rest[0][1]]
            elif token == "import" and len(rest) > 1 and rest[0][1] == "(" and rest[1][0] == "string":
                specs, kind = [rest[1][1]], "literal dynamic import"
            else:
                for offset, t in enumerate(rest):
                    if t[1] == ";" or (t[1] in {"import", "export"} and offset > 0):
                        break
                    if t[1] == "from" and offset + 1 < len(rest) and rest[offset + 1][0] == "string":
                        specs = [rest[offset + 1][1]]
                        break
            for spec in specs:
                target = resolve(path, spec)
                if target:
                    adjacency[path].add(target)
                    edges.append({"from": path, "to": target, "line": line, "kind": kind})
                elif spec.startswith(".") and not PurePosixPath(spec).suffix in {".css", ".svg", ".png", ".json"}:
                    unresolved.append({"path": path, "line": line, "specifier": spec, "kind": kind})
        # JSX prose can contain an apostrophe that a general JS token scanner
        # treats as a quote. A literal-import fallback deliberately overcounts
        # rather than calling a lazy panel dead because of prose before it.
        # Comment-only matches are possible and are labelled conservatively.
        for match in re.finditer(r"\bimport\s*\(\s*['\"](\.[^'\"]+)['\"]\s*\)", texts[path]):
            target = resolve(path, match.group(1))
            if target and target not in adjacency[path]:
                adjacency[path].add(target)
                edges.append({"from": path, "to": target, "line": texts[path].count("\n", 0, match.start()) + 1,
                              "kind": "literal dynamic import (conservative JSX fallback; comment matches possible)"})

    reached, queue = set(), deque(["src/main.tsx"])
    while queue:
        current = queue.popleft()
        if current in reached or current not in modules:
            continue
        reached.add(current)
        queue.extend(sorted(adjacency[current] - reached))
    selected_evidence = []
    for candidate in ("src/constants.ts", "src/lib/a11y.ts", "src/components/HandoffPrintView.tsx"):
        stem = PurePosixPath(candidate).stem
        references = [{"path": p, "line": number, "text": line.strip()[:250]}
                      for p, text in texts.items() if p != candidate
                      for number, line in enumerate(text.splitlines(), 1)
                      if (stem in line and (stem != "constants" or "constants'" in line or 'constants"' in line))]
        selected_evidence.append({"path": candidate, "exists": candidate in texts, "reachable_from_main": candidate in reached,
                                  "incoming_production_edges": [e for e in edges if e["to"] == candidate],
                                  "text_references_including_tests_docs": references})
    return {"method": "Conservative stdlib lexical import scanner with literal dynamic imports and literal import.meta.glob expansion. JSX fallback can include comment-only literal dynamic imports. Includes type-only edges; does not evaluate computed imports or runtime gates. Unreachable is a review candidate, not a deletion instruction.",
            "entrypoint": "src/main.tsx", "module_count": len(modules), "reachable_count": len(reached),
            "unreachable_candidates": sorted(modules - reached), "unresolved_relative_import_candidates": unresolved,
            "selected_cleanup_evidence": selected_evidence, "edges": edges}


def dotted(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (ValueError, AttributeError):
        return "<unresolved>"


def literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def python_inventory(texts: dict[str, str]) -> dict:
    routes, classes, services, placeholders, passes, errors, router_mounts = [], [], [], [], [], [], []
    for path, text in texts.items():
        if not path.startswith("server/") or not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as error:
            errors.append({"path": path, "line": error.lineno, "error": error.msg})
            continue
        routers = {}
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call) and dotted(node.value.func).endswith("APIRouter"):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                prefix = next((literal_string(k.value) for k in node.value.keywords if k.arg == "prefix"), "")
                for target in targets:
                    routers[dotted(target)] = prefix
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
                router_mounts.append({"path": path, "line": node.lineno, "router": dotted(node.args[0]) if node.args else None,
                                      "prefix": next((literal_string(k.value) for k in node.keywords if k.arg == "prefix"), ""),
                                      "dependencies": [dotted(k.value) for k in node.keywords if k.arg == "dependencies"]})
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not is_test(path):
                classes.append({"path": path, "line": node.lineno, "name": node.name, "bases": [dotted(b) for b in node.bases],
                                "decorators": [dotted(d) for d in node.decorator_list],
                                "fields": [{"name": dotted(f.target), "annotation": dotted(f.annotation), "line": f.lineno}
                                           for f in node.body if isinstance(f, ast.AnnAssign)]})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if production(path):
                    for decorator in node.decorator_list:
                        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in HTTP_METHODS:
                            owner = dotted(decorator.func.value)
                            route_path = literal_string(decorator.args[0]) if decorator.args else None
                            prefix = routers.get(owner, "")
                            routes.append({"path": path, "line": decorator.lineno, "handler": node.name, "async": isinstance(node, ast.AsyncFunctionDef),
                                           "method": decorator.func.attr.upper(), "router": owner, "declared_path": route_path,
                                           "router_prefix": prefix, "candidate_url": (prefix + route_path) if isinstance(prefix, str) and route_path is not None else None,
                                           "response_model": next((dotted(k.value) for k in decorator.keywords if k.arg == "response_model"), None)})
                    if path.startswith("server/services/") and isinstance(parents.get(node), ast.Module):
                        services.append({"path": path, "line": node.lineno, "name": node.name, "async": isinstance(node, ast.AsyncFunctionDef),
                                         "public_name": not node.name.startswith("_"), "lines": (node.end_lineno or node.lineno) - node.lineno + 1})
                body = node.body[1:] if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str) else node.body
                if len(body) == 1 and (isinstance(body[0], ast.Pass) or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and body[0].value.value is Ellipsis)
                                       or (isinstance(body[0], ast.Raise) and body[0].exc and "NotImplemented" in dotted(body[0].exc))):
                    owner = parents.get(node)
                    decorators = [dotted(d) for d in node.decorator_list]
                    owner_bases = [dotted(b) for b in owner.bases] if isinstance(owner, ast.ClassDef) else []
                    if is_test(path):
                        interpretation = "test fixture/mock declaration; not production application logic"
                    elif any("abstractmethod" in d for d in decorators):
                        interpretation = "abstract interface contract; concrete subclasses supply behavior"
                    elif any("Protocol" in base for base in owner_bases):
                        interpretation = "typing Protocol contract; not a runtime handler"
                    elif node.name == "__init__":
                        interpretation = "empty constructor; not evidence that other implemented methods are stubbed"
                    else:
                        interpretation = "review candidate; not automatically missing application logic"
                    placeholders.append({"path": path, "line": node.lineno, "name": node.name, "test": is_test(path),
                                         "body_kind": type(body[0]).__name__, "decorators": decorators,
                                         "owner_class": owner.name if isinstance(owner, ast.ClassDef) else None,
                                         "owner_bases": owner_bases, "interpretation": interpretation})
            if isinstance(node, ast.Pass):
                ancestor = parents.get(node)
                in_exception = False
                while ancestor is not None:
                    if isinstance(ancestor, ast.ExceptHandler):
                        in_exception = True
                        break
                    if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        break
                    ancestor = parents.get(ancestor)
                passes.append({"path": path, "line": node.lineno, "test": is_test(path),
                               "context": "exception guard" if in_exception else type(parents.get(node)).__name__})
    model_names = {"BaseModel", "pydantic.BaseModel", "SQLModel", "BaseSettings"}
    changed = True
    while changed:
        previous = set(model_names)
        model_names.update(c["name"] for c in classes if set(c["bases"]) & model_names)
        changed = previous != model_names
    models = [c for c in classes if set(c["bases"]) & model_names or any("dataclass" in d for d in c["decorators"])]
    return {"method": "Python stdlib AST, source only; no imports, DB connection, server startup, or annotation evaluation. URLs combine literal same-file APIRouter prefixes with decorators; mounting/gates may alter runtime availability.",
            "routes": routes, "router_mounts": router_mounts, "model_candidates": models, "services_top_level_functions": services,
            "placeholder_body_candidates": placeholders, "pass_statements": passes,
            "pass_context_counts": dict(Counter(("test: " if p["test"] else "production: ") + p["context"] for p in passes)),
            "parse_errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("docs/audit-2026-10/repository-inventory.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    paths = sorted(set(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")) - {""})
    texts, manifest, skipped = {}, [], Counter()
    for path in paths:
        full = root / path
        reason = exclusion(path, full)
        if reason:
            skipped[reason] += 1
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            skipped["unreadable/non-UTF8 text"] += 1
            continue
        if "\0" in text:
            skipped["binary NUL bytes"] += 1
            continue
        texts[path] = text
        manifest.append({"path": path, "bytes": full.stat().st_size, "lines": len(text.splitlines()), "category": category(path), "production_code": production(path)})
    by_category, by_directory = {}, {}
    for key, label in (("category", by_category), ("directory", by_directory)):
        groups = defaultdict(list)
        for item in manifest:
            group = item["category"] if key == "category" else "/".join(PurePosixPath(item["path"]).parts[:2]) if "/" in item["path"] else "(root)"
            groups[group].append(item)
        label.update({group: {"files": len(items), "lines": sum(i["lines"] for i in items), "bytes": sum(i["bytes"] for i in items)} for group, items in sorted(groups.items())})
    tables = []
    table_pattern = re.compile(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)", re.I)
    for path, text in texts.items():
        if production(path) and path.endswith((".py", ".sql")):
            for match in table_pattern.finditer(text):
                tables.append({"path": path, "line": text.count("\n", 0, match.start()) + 1, "name": match.group(1)})
    markers = marker_inventory(texts)
    backend = python_inventory(texts)
    graph = ts_graph(texts, root)
    result = {"schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "git_head": git(root, "rev-parse", "HEAD").strip(),
              "scope": {"listing": "git ls-files --cached --others --exclude-standard; deduplicated; current working files including pre-existing edits", "excluded_directory_names": sorted(EXCLUDED_PARTS),
                        "allowed_text_extensions": sorted(TEXT_SUFFIXES), "exclusions": dict(skipped),
                        "limitations": ["No environment, secret, DB, clinical runtime data, backup, binary, dependency, or symlink-target contents read.",
                                        "This inventory is static evidence and does not establish runtime correctness, test coverage, authorization, clinical safety, or feature completeness.",
                                        "Generated audit evidence and this generator excluded to avoid recursive self-inventory.",
                                        "return [], pass, optional fallbacks and feature gates are not classified as bugs by syntax alone."]},
              "counts": {"git_listed_paths": len(paths), "inspected_text_files": len(manifest), "inspected_text_lines": sum(i["lines"] for i in manifest),
                         "production_code_files": sum(i["production_code"] for i in manifest), "production_code_lines": sum(i["lines"] for i in manifest if i["production_code"]),
                         "route_decorator_candidates": len(backend["routes"]), "model_candidates": len(backend["model_candidates"]),
                         "service_functions": len(backend["services_top_level_functions"]), "unique_create_table_candidates": len({t["name"] for t in tables})},
              "by_category": by_category, "by_directory": by_directory, "files": manifest, "markers": markers,
              "frontend_import_graph": graph, "backend_ast": backend,
              "sql_table_candidates": {"method": "Literal CREATE TABLE regex in production Python/SQL source; includes migrations, replacement/temp tables, repeated schemas and comments; no database opened.",
                                       "unique_names": sorted({t["name"] for t in tables}), "occurrences": tables}}
    destination = args.output if args.output.is_absolute() else root / args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(destination), **result["counts"], "frontend_modules": graph["module_count"], "frontend_reachable": graph["reachable_count"],
                      "frontend_unreachable_candidates": graph["unreachable_candidates"], "production_marker_counts": markers["production_marker_counts"], "python_parse_errors": len(backend["parse_errors"])}, indent=2))


if __name__ == "__main__":
    main()
