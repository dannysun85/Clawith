"""Regression coverage for strict MCP server argument schemas."""

from app.services.agent_tools import _coerce_mcp_arguments


def test_coerces_only_explicit_schema_types_recursively():
    arguments = {
        "server_id": "12345",
        "cpu": "2.5",
        "enabled": "TRUE",
        "ports": ["22", "443"],
        "nested": {"retries": "3"},
        "label": "007",
        "unknown": "42",
    }
    schema = {
        "type": "object",
        "properties": {
            "server_id": {"type": "integer"},
            "cpu": {"type": "number"},
            "enabled": {"type": "boolean"},
            "ports": {"type": "array", "items": {"type": "integer"}},
            "nested": {
                "type": "object",
                "properties": {"retries": {"type": ["integer", "null"]}},
            },
            "label": {"type": "string"},
        },
    }

    assert _coerce_mcp_arguments(arguments, schema) == {
        "server_id": 12345,
        "cpu": 2.5,
        "enabled": True,
        "ports": [22, 443],
        "nested": {"retries": 3},
        "label": "007",
        "unknown": "42",
    }
    assert arguments["server_id"] == "12345"


def test_invalid_or_ambiguous_values_are_left_for_server_validation():
    arguments = {
        "server_id": "12.5",
        "ratio": "nan",
        "flag": "yes",
        "choice": "10",
    }
    schema = {
        "type": "object",
        "properties": {
            "server_id": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "choice": {"type": ["integer", "string"]},
        },
    }

    assert _coerce_mcp_arguments(arguments, schema) == arguments
