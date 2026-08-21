import pytest

from app.scripts.inventory_production_governance import (
    assert_identity_free_report,
    summarize_ceo_previews,
)


def test_ceo_inventory_aggregates_without_returning_tenant_or_agent_identity():
    report = summarize_ceo_previews(
        [
            {
                "tenant_id": "tenant-one",
                "classification": "none",
                "candidates": [],
                "warnings": [],
            },
            {
                "tenant_id": "tenant-two",
                "classification": "legacy_contaminated_archive",
                "candidates": [{"agent_id": "agent-one"}],
                "warnings": ["manual review"],
            },
            {
                "tenant_id": "tenant-three",
                "classification": "legacy_contaminated_archive",
                "candidates": [{"agent_id": "agent-two"}],
                "warnings": ["manual review"],
            },
        ]
    )

    assert report == {
        "active_tenants_scanned": 3,
        "classification_counts": {
            "legacy_contaminated_archive": 2,
            "none": 1,
        },
        "candidate_count": 2,
        "warning_count": 2,
        "automatic_adoption_allowed": False,
        "automatic_archive_allowed": False,
    }
    assert "tenant-one" not in str(report)
    assert "agent-one" not in str(report)
    assert_identity_free_report(report)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"tenant_id": "secret"},
        {"nested": [{"order_id": "secret"}]},
        {"provider_receipt_id": "secret"},
        {"summary": "may contain customer content"},
        {"route": "/agents/customer-id/chat"},
    ],
)
def test_inventory_privacy_guard_rejects_identity_bearing_fields(unsafe):
    with pytest.raises(ValueError, match="identity-bearing"):
        assert_identity_free_report(unsafe)


def test_inventory_privacy_guard_accepts_only_aggregate_operational_dimensions():
    assert_identity_free_report(
        {
            "read_only": True,
            "media_provider_debt": [
                {
                    "status": "provider_inflight",
                    "action": "chat",
                    "records": 13,
                    "held_credits": 357,
                    "age_bucket": ">7d",
                }
            ],
            "manual_pending_orders": [{"type": "subscribe", "records": 22}],
            "open_production_issues": [
                {"source": "client_api", "error_code": "TypeError", "events": 153}
            ],
        }
    )
