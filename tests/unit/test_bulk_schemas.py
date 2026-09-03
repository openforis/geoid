"""Unit tests for the bulk request/response schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.schemas.place import (
    BulkAccepted,
    BulkFeatureCollection,
    BulkRejected,
    BulkReport,
    BulkSummary,
)

pytestmark = pytest.mark.unit


def test_envelope_rejects_empty_features():
    # An empty FeatureCollection is a 422 at the body boundary, never a 200 report.
    with pytest.raises(ValidationError):
        BulkFeatureCollection(type="FeatureCollection", features=[])


def test_envelope_rejects_wrong_type():
    with pytest.raises(ValidationError):
        BulkFeatureCollection(type="Feature", features=[{"type": "Feature"}])


def test_envelope_keeps_features_as_raw_dicts():
    # Features stay RAW dicts — each is validated per-feature in the service (so one
    # malformed feature is one rejected row), not at the envelope.
    fc = BulkFeatureCollection(
        type="FeatureCollection", features=[{"anything": True}, {"type": "Feature"}]
    )
    assert fc.features == [{"anything": True}, {"type": "Feature"}]


def test_report_summary_counts_add_up():
    report = BulkReport(
        summary=BulkSummary(received=3, accepted=2, rejected=1),
        accepted=[
            BulkAccepted(index=0, geoid="g0", uri="u0"),
            BulkAccepted(index=1, geoid="g1", uri="u1", external_id="x1"),
        ],
        rejected=[
            BulkRejected(
                index=2,
                reason="external_id_conflict",
                detail="external_id already exists in this collection",
                external_id="x1",
            )
        ],
    )
    assert report.summary.received == len(report.accepted) + len(report.rejected)
    assert report.summary.accepted == len(report.accepted)
    assert report.summary.rejected == len(report.rejected)


def test_reject_reasons_no_longer_include_geometry_conflict():
    # Mint is idempotent: a duplicate geometry is ACCEPTED with the incumbent
    # geoid, so the reason no longer exists at the schema level.
    with pytest.raises(ValidationError):
        BulkRejected(index=0, reason="geometry_conflict")
