"""Static checks on the YAML config (no database needed)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


def test_schema_docs_structure():
    tables = _load("schema_docs.yaml")["tables"]
    assert len(tables) == 7
    for name, spec in tables.items():
        assert spec["description"], name
        assert spec["columns"], name
        for col, meta in spec["columns"].items():
            assert meta.get("description"), f"{name}.{col}"


def test_glossary_structure():
    g = _load("glossary.yaml")
    assert g["terms"]
    for term, spec in g["terms"].items():
        assert spec["definition"].strip(), term
    # terms that must stay undefined so they trigger clarification
    undefined = {"best customer", "top customers", "recently", "large order", "high value", "loyal"}
    assert not undefined & set(g["terms"])
