from jsonschema import Draft202012Validator

from copperchat.tools import registry

EXPECTED = {
    "project_info",
    "sch_list_symbols",
    "sch_get_symbol",
    "sch_get_net",
    "pcb_list_footprints",
    "pcb_get_footprint",
    "pcb_get_net",
    "run_erc",
    "run_drc",
    "render_view",
}


def test_all_phase4_tools_registered() -> None:
    specs = registry.all_specs()
    names = [s.name for s in specs]
    assert set(names) >= EXPECTED and len(names) == len(set(names))
    for s in specs:
        if s.name in EXPECTED:
            Draft202012Validator.check_schema(s.input_schema)
            assert s.input_schema["type"] == "object"
            assert s.input_schema.get("additionalProperties") is False
            assert not s.mutating and not s.changes_documents
            assert len(s.description) > 30


def test_required_args() -> None:
    by = {s.name: s for s in registry.all_specs()}
    assert by["sch_get_symbol"].input_schema["required"] == ["ref"]
    assert by["pcb_get_net"].input_schema["required"] == ["name"]
    assert by["render_view"].input_schema["required"] == ["doc", "mode"]
    validator = Draft202012Validator(by["render_view"].input_schema)
    assert validator.is_valid({"doc": "pcb", "mode": "agent", "region": {"around_ref": "U1"}})
    assert not validator.is_valid({"doc": "pcb", "mode": "agent", "region": {"w_mm": 0}})
    assert not validator.is_valid({"doc": "pcb", "mode": "agent", "zoom": 2})
