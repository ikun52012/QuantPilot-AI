"""Alembic migration metadata tests."""

import importlib.util
from pathlib import Path


def _load_revision_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alembic_revision_ids_fit_version_table():
    versions_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"

    for path in versions_dir.glob("*.py"):
        module = _load_revision_module(path)
        revision = getattr(module, "revision", "")
        assert len(revision) <= 32, f"{path.name} revision id exceeds alembic_version.version_num length"
