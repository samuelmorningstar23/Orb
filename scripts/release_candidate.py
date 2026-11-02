#!/usr/bin/env python3
"""Offline release-candidate identity and frontend packaging; never publishes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

TAG = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    ).strip()


def identity(root: Path, tag: str, expected_sha: str | None = None) -> dict[str, str]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(
            "Candidate tag must have the form vMAJOR.MINOR.PATCH with an optional prerelease"
        )
    prerelease = match.group(4)
    if prerelease and any(
        part.isdigit() and len(part) > 1 and part[0] == "0" for part in prerelease.split(".")
    ):
        raise ValueError("Candidate prerelease numeric identifiers cannot contain leading zeros")
    sha = git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("Candidate commit identity is invalid")
    if expected_sha is not None and (
        sha != expected_sha or git(root, "rev-parse", "HEAD") != expected_sha
    ):
        raise ValueError("Candidate tag or checkout changed after verification")
    package: object = json.loads(git(root, "show", f"{sha}:package.json"))
    if not isinstance(package, dict) or package.get("version") != tag[1:]:
        raise ValueError("Candidate tag does not match package.json version at that commit")
    return {"tag": tag, "commit": sha, "version": tag[1:]}


def bundle(root: Path, tag: str, expected_sha: str, run_id: str = "local") -> Path:
    details = identity(root, tag, expected_sha)
    dist = root / "dist"
    if dist.is_symlink() or not (dist / "index.html").is_file():
        raise ValueError("Candidate frontend build is missing")
    output = root / "release-candidate"
    if output.exists():
        raise ValueError("Refusing to replace an existing candidate bundle")
    files = sorted(dist.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in files):
        raise ValueError("Candidate build must contain only regular files and directories")
    assets = [
        {
            "path": path.relative_to(dist).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in files
        if path.is_file()
    ]
    manifest = {
        "schema": "orb.release.candidate/1",
        **details,
        "built_utc": datetime.now(UTC).isoformat(),
        "automated_gate_run": run_id,
        "artifact": "frontend-only",
        "assets": assets,
        "acceptance": {
            "clinical": "not_assessed",
            "site_restore": "not_assessed",
            "hardware": "not_assessed",
            "signing": "not_performed",
            "deployment": "not_performed",
        },
    }
    with tempfile.TemporaryDirectory(prefix=".orb-candidate-", dir=root) as directory:
        stage = Path(directory)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive = stage / f"orb-{tag}-frontend.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in files:
                if path.is_file():
                    tar.add(
                        path,
                        arcname="frontend/" + path.relative_to(dist).as_posix(),
                        recursive=False,
                    )
            tar.add(stage / "manifest.json", arcname="manifest.json", recursive=False)
        hashes = [
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (archive, stage / "manifest.json")
        ]
        (stage / "SHA256SUMS").write_text("".join(hashes))
        stage.rename(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("resolve", "bundle"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    tag = os.environ.get("ORB_CANDIDATE_TAG", "")
    if args.mode == "resolve":
        result = identity(root, tag)
        target = os.environ.get("GITHUB_OUTPUT")
        if target:
            with open(target, "a", encoding="utf-8") as stream:
                stream.write(f"sha={result['commit']}\ntag={result['tag']}\n")
        print(json.dumps(result, sort_keys=True))
    else:
        expected = os.environ.get("ORB_CANDIDATE_SHA", "")
        if not expected:
            parser.error("ORB_CANDIDATE_SHA must identify the commit that passed every gate")
        bundle(root, tag, expected, os.environ.get("GITHUB_RUN_ID", "local"))


if __name__ == "__main__":
    main()
