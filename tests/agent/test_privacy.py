import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_public_tree.py"
spec = importlib.util.spec_from_file_location("public_tree", SCRIPT)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_sensitive_paths_and_symlinks_are_rejected():
    for name in (
        ".env",
        "agent.toml",
        ".agent-runs/run/prompt.txt",
        "exports/out.json",
        "data/startups/_rejected/draft.toml",
    ):
        assert "sensitive file path" in checker.problems(name, b"ordinary text")
    assert checker.problems("src/link.py", b"../private/file", "120000")


def test_credentials_are_reported_without_values():
    secret = "sk-" + "proj-" + "a1B2c3D4" * 8
    findings = checker.problems("src/example.py", secret.encode())
    assert findings and all(secret not in finding for finding in findings)
    home = "/" + "Users" + "/example/private/file"
    assert checker.problems("README.md", home.encode())
    credential_url = "https://" + "user:pass" + "@example.com/"
    assert checker.problems("README.md", credential_url.encode())


def test_public_source_urls_and_package_hashes_are_allowed():
    assert not checker.problems(
        "data/startups/example.toml",
        b'url = "https://www.businesswire.com/news/home/12345/en/example"',
    )
    assert not checker.problems("uv.lock", b'hash = "sha256:' + b"a" * 64 + b'"')


def test_check_uses_indexed_blob_even_after_worktree_cleanup(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    file = tmp_path / "example.py"
    secret = "gh" + "p_" + "Ab9" * 20
    file.write_text(secret)
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, check=True)
    file.write_text("clean working copy\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "example.py: line 1: provider token" in result.stdout
    assert secret not in result.stdout
