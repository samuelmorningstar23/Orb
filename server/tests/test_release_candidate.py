"""Release candidate packaging in an owned synthetic repository, offline."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("orb_release_candidate", Path(__file__).resolve().parents[2] / "scripts/release_candidate.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "package.json").write_text(json.dumps({"name": "synthetic", "version": "1.2.3"}))
    subprocess.run(["git", "-C", str(tmp_path), "add", "package.json"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "Synthetic fixture"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v1.2.3"], check=True)
    return tmp_path, release.git(tmp_path, "rev-parse", "HEAD")


@pytest.mark.parametrize("tag", ["../main", "main", "v01.2.3", "v1.2", "v1.2.3;echo unsafe", "v1.2.3\n", "v1.2.3-01"])
def test_invalid_tag_is_refused_before_git_lookup(repository, tag):
    root, _ = repository
    with pytest.raises(ValueError):
        release.identity(root, tag)


def test_version_and_verified_commit_must_match(repository):
    root, sha = repository
    assert release.identity(root, "v1.2.3", sha)["commit"] == sha
    subprocess.run(["git", "-C", str(root), "tag", "v1.2.4"], check=True)
    with pytest.raises(ValueError, match="version"):
        release.identity(root, "v1.2.4")
    with pytest.raises(ValueError, match="changed"):
        release.identity(root, "v1.2.3", "0" * 40)


def test_bundle_contains_only_frontend_and_explicit_acceptance_limits(repository):
    root, sha = repository
    (root / "dist/assets").mkdir(parents=True)
    (root / "dist/index.html").write_text("<p>Synthetic build</p>")
    (root / "dist/assets/app.js").write_text("console.log('synthetic')")
    (root / ".env").write_text("must never enter candidate")
    (root / "orb.db").write_bytes(b"must never enter candidate")
    output = release.bundle(root, "v1.2.3", sha, "synthetic-run")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["commit"] == sha and manifest["automated_gate_run"] == "synthetic-run"
    assert manifest["acceptance"]["clinical"] == "not_assessed"
    archive = next(output.glob("*.tar.gz"))
    with tarfile.open(archive) as tar:
        assert set(tar.getnames()) == {"frontend/index.html", "frontend/assets/app.js", "manifest.json"}
    for line in (output / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected
    with pytest.raises(ValueError, match="existing"):
        release.bundle(root, "v1.2.3", sha)


def test_symlink_and_missing_build_are_refused(repository):
    root, sha = repository
    with pytest.raises(ValueError, match="missing"):
        release.bundle(root, "v1.2.3", sha)
    (root / "dist").mkdir()
    (root / "dist/index.html").write_text("synthetic")
    (root / "dist/foreign").symlink_to(root / "package.json")
    with pytest.raises(ValueError, match="regular files"):
        release.bundle(root, "v1.2.3", sha)


def test_invalid_resolved_commit_is_refused(repository, monkeypatch):
    root, _ = repository
    monkeypatch.setattr(release, "git", lambda *_args: "not-a-commit")
    with pytest.raises(ValueError, match="identity is invalid"):
        release.identity(root, "v1.2.3")


def test_non_object_package_is_refused(repository, monkeypatch):
    root, _ = repository
    original_git = release.git
    monkeypatch.setattr(
        release,
        "git",
        lambda root, *args: "[]" if args[0] == "show" else original_git(root, *args),
    )
    with pytest.raises(ValueError, match="package.json version"):
        release.identity(root, "v1.2.3")


def test_changed_checkout_is_refused_even_if_tag_is_unchanged(repository):
    root, sha = repository
    (root / "later.txt").write_text("Changed after verification")
    subprocess.run(["git", "-C", str(root), "add", "later.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "Later fixture"],
        check=True,
    )
    with pytest.raises(ValueError, match="checkout changed"):
        release.identity(root, "v1.2.3", sha)


@pytest.mark.parametrize("github_output", [False, True])
def test_resolve_cli_outputs_verified_identity(repository, monkeypatch, capsys, github_output):
    root, sha = repository
    monkeypatch.setattr(release, "__file__", str(root / "scripts/release_candidate.py"))
    monkeypatch.setattr("sys.argv", ["release_candidate.py", "resolve"])
    monkeypatch.setenv("ORB_CANDIDATE_TAG", "v1.2.3")
    output = root / "github-output.txt"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    if github_output:
        output.write_text("earlier=preserved\n")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    release.main()
    assert json.loads(capsys.readouterr().out) == {
        "tag": "v1.2.3", "commit": sha, "version": "1.2.3"
    }
    if github_output:
        assert output.read_text() == f"earlier=preserved\nsha={sha}\ntag=v1.2.3\n"
    else:
        assert not output.exists()


def test_bundle_cli_requires_a_verified_commit_before_creating_output(repository, monkeypatch, capsys):
    root, _ = repository
    monkeypatch.setattr(release, "__file__", str(root / "scripts/release_candidate.py"))
    monkeypatch.setattr("sys.argv", ["release_candidate.py", "bundle"])
    monkeypatch.setenv("ORB_CANDIDATE_TAG", "v1.2.3")
    monkeypatch.delenv("ORB_CANDIDATE_SHA", raising=False)
    with pytest.raises(SystemExit) as error:
        release.main()
    assert error.value.code == 2
    assert "must identify the commit that passed every gate" in capsys.readouterr().err
    assert not (root / "release-candidate").exists()


def test_bundle_cli_records_gate_run_identity(repository, monkeypatch):
    root, sha = repository
    (root / "dist").mkdir()
    (root / "dist/index.html").write_text("Synthetic candidate")
    monkeypatch.setattr(release, "__file__", str(root / "scripts/release_candidate.py"))
    monkeypatch.setattr("sys.argv", ["release_candidate.py", "bundle"])
    monkeypatch.setenv("ORB_CANDIDATE_TAG", "v1.2.3")
    monkeypatch.setenv("ORB_CANDIDATE_SHA", sha)
    monkeypatch.setenv("GITHUB_RUN_ID", "verified-synthetic-run")
    release.main()
    manifest = json.loads((root / "release-candidate/manifest.json").read_text())
    assert manifest["commit"] == sha
    assert manifest["automated_gate_run"] == "verified-synthetic-run"
