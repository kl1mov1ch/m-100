import importlib.util
from pathlib import Path
import subprocess


def test_secret_check_reads_git_index_instead_of_only_worktree(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "publication_check", Path(__file__).parents[1] / "scripts/check_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    path = tmp_path / "example.py"
    path.write_text('secret="' + "gsk_" + "A" * 45 + '"', encoding="utf-8")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, check=True)
    path.write_text('value = "safe"', encoding="utf-8")
    assert not module.scan()
    assert ("example.py", "Groq key") in module.scan(staged=True)
