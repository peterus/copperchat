from copperchat.errors import StructuredToolError


def test_code_and_details_only_when_set() -> None:
    plain = StructuredToolError("w", "y", "s")
    assert plain.to_dict() == {"what": "w", "why": "y", "suggestion": "s"}
    assert plain.code == "tool_error"
    err = StructuredToolError("w", "y", "s", code="not_found", details={"close": ["a"]})
    assert err.code == "not_found" and StructuredToolError.code == "tool_error"
    expected = {"what": "w", "why": "y", "suggestion": "s", "code": "not_found"}
    assert err.to_dict() == {**expected, "details": {"close": ["a"]}}
