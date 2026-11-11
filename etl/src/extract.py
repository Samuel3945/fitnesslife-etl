"""Extract: read-only queries against the MongoDB source.

Projections exclude secrets (password, QR tokens, payment signatures) so they
never leave the source database.
"""

EXCLUDED_FIELDS = {"password": 0, "qrToken": 0, "qrCodePath": 0, "signature": 0, "approvalCode": 0}

COLLECTIONS = ["users", "plans", "payments", "attendances", "functionalTrainings", "churnPredictions"]


def extract_all(db) -> dict[str, list[dict]]:
    return {name: list(db[name].find({}, EXCLUDED_FIELDS)) for name in COLLECTIONS}
