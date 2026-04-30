import importlib.util
import sys
from pathlib import Path


def _load_updater_module(tmp_path):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "docker-updater.py"
    module_name = "docker_updater_test_module"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.BASE_DIR = tmp_path
    module.ENV_FILE = tmp_path / ".env"
    return module


def test_persist_target_images_updates_env_file(tmp_path):
    updater = _load_updater_module(tmp_path)
    updater.ENV_FILE.write_text("SIGNAL_SERVER_IMAGE=old\n", encoding="utf-8")

    updater.persist_target_images(
        "ghcr.io/ikun52012/quantpilot-ai:v4.5.4",
        "ghcr.io/ikun52012/quantpilot-ai-updater:v4.5.4",
    )

    content = updater.ENV_FILE.read_text(encoding="utf-8")
    assert "SIGNAL_SERVER_IMAGE=ghcr.io/ikun52012/quantpilot-ai:v4.5.4" in content
    assert "SIGNAL_UPDATER_IMAGE=ghcr.io/ikun52012/quantpilot-ai-updater:v4.5.4" in content


def test_wait_for_rollout_accepts_running_service_with_matching_version(tmp_path, monkeypatch):
    updater = _load_updater_module(tmp_path)
    monkeypatch.setattr(
        updater,
        "_service_status",
        lambda: {"State": "running", "Status": "Up 5 seconds (healthy)"},
    )
    monkeypatch.setattr(updater, "_health_payload", lambda: {"status": "healthy", "version": "4.5.4"})

    result = updater.wait_for_rollout("4.5.4", "ghcr.io/ikun52012/quantpilot-ai:v4.5.4")

    assert result["status"] == "completed"
    assert result["observed_version"] == "4.5.4"
