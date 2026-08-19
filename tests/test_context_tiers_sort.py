import pytest
from pathlib import Path
from lanegate.executor import build_executor_cmd

def test_context_tiers_sort_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)
    (tmp_path / "file.py").write_text("x" * 150)
    cfg = {
        "executors": {
            "aider": {
                "context_tiers": [
                    {"tokens": 40000, "model": "large-model"},
                    {"tokens": 10000, "model": "small-model"},
                ]
            }
        }
    }
    cmd = build_executor_cmd(
        "aider", "prompt", cfg, touches=["file.py"], worktree_path=tmp_path
    )
    # The prompt and file are small, so small-model should be selected
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "small-model"
