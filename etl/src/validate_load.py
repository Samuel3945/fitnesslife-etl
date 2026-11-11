"""Validate the published warehouse against the MongoDB source.

Expected values are recomputed directly from MongoDB (independently of the
transform code) and compared with what Azure SQL holds. Exit code 1 if any
check fails.

Usage:
    python src/validate_load.py
"""

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bson import DBRef

from config import load_settings
from db import azure_engine, mongo_database, wait_for_azure

checks: list[tuple[str, bool, str]] = []


def check(name: str, expected, actual) -> None:
    checks.append((name, expected == actual, f"expected={expected} actual={actual}"))


def ref(value) -> str:
    return str(value.id) if isinstance(value, DBRef) else str(value)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def source_time(value: datetime, settings) -> datetime:
    if settings.source_timezone is None:
        return value
    return value.replace(tzinfo=timezone.utc).astimezone(settings.source_timezone).replace(tzinfo=None)


def main() -> int:
    settings = load_settings()
    client, db = mongo_database(settings)
    users = list(db.users.find({}, {"identification": 1, "name": 1}))
    plans = list(db.plans.find({}, {"planName": 1}))
    payments = list(db.payments.find({}, {"amount": 1, "status": 1, "plan": 1, "user": 1}))
    attendances = list(db.attendances.find({}, {"checkIn": 1, "checkOut": 1, "user": 1}))
    trainings = list(db.functionalTrainings.find({}, {"idFunctionalTraining": 1, "userIds": 1}))
    churn = list(db.churnPredictions.find({}, {"riskLevel": 1, "prediction": 1, "userId": 1, "calculatedAt": 1}))
    client.close()

    plan_name_by_id = {str(p["_id"]): p["planName"] for p in plans}
    engine = azure_engine(settings)
    wait_for_azure(engine)
    with engine.connect() as conn:
        q = lambda sql: conn.exec_driver_sql(sql)  # noqa: E731

        # Dimensions
        check("DimUser rows (without key 0)", len(users), q("SELECT COUNT(*) FROM dw.DimUser WHERE UserKey > 0").scalar())
        check("DimPlan rows (without key 0)", len(plans), q("SELECT COUNT(*) FROM dw.DimPlan WHERE PlanKey > 0").scalar())
        check("DimTraining rows (without key 0)", len(trainings), q("SELECT COUNT(*) FROM dw.DimTraining WHERE TrainingKey > 0").scalar())
        check("DimPlan names (accents preserved)", sorted(plan_name_by_id.values()),
              sorted(q("SELECT PlanName FROM dw.DimPlan WHERE PlanKey > 0").scalars().all()))
        span = q("SELECT DATEDIFF(day, MIN(FullDate), MAX(FullDate)) + 1, COUNT(*) FROM dw.DimDate").fetchone()
        check("DimDate is contiguous (no gaps)", span[0], span[1])
        check("DimTime has 24 hours", 24, q("SELECT COUNT(*) FROM dw.DimTime").scalar())

        facts = dict(q(
            "SELECT e.EventType, COUNT(*) FROM dw.FactMemberActivity f "
            "JOIN dw.DimEventType e ON e.EventTypeKey = f.EventTypeKey GROUP BY e.EventType"
        ).fetchall())

        # Attendances
        check("Attendance rows", len(attendances), facts.get("ATTENDANCE"))
        expected_minutes = sum(round((a["checkOut"] - a["checkIn"]).total_seconds() / 60) for a in attendances if a.get("checkOut"))
        check("Total attendance minutes", expected_minutes, q("SELECT SUM(AttendanceMinutes) FROM dw.FactMemberActivity").scalar())
        check("Distinct users with attendance", len({ref(a["user"]) for a in attendances}), q(
            "SELECT COUNT(DISTINCT u.UserIdMongo) FROM dw.FactMemberActivity f JOIN dw.DimUser u ON u.UserKey = f.UserKey "
            "JOIN dw.DimEventType e ON e.EventTypeKey = f.EventTypeKey WHERE e.EventType = 'ATTENDANCE'").scalar())

        # Payments
        check("Payment rows", len(payments), facts.get("PAYMENT"))
        check("Total payment amount", money(sum((Decimal(str(p["amount"])) for p in payments), Decimal(0))),
              money(q("SELECT SUM(PaymentAmount) FROM dw.FactMemberActivity").scalar()))
        expected_by_plan: dict[str, Decimal] = {}
        for p in payments:
            name = plan_name_by_id[ref(p["plan"])]
            expected_by_plan[name] = expected_by_plan.get(name, Decimal(0)) + Decimal(str(p["amount"]))
        actual_by_plan = {name: money(total) for name, total in q(
            "SELECT pl.PlanName, SUM(f.PaymentAmount) FROM dw.FactMemberActivity f JOIN dw.DimPlan pl ON pl.PlanKey = f.PlanKey "
            "WHERE f.PaymentAmount IS NOT NULL GROUP BY pl.PlanName").fetchall()}
        check("Payment amount by plan", {k: money(v) for k, v in expected_by_plan.items()}, actual_by_plan)
        check("Payments by status", dict(Counter(p["status"] for p in payments)), dict(q(
            "SELECT e.EventStatus, COUNT(*) FROM dw.FactMemberActivity f JOIN dw.DimEventType e ON e.EventTypeKey = f.EventTypeKey "
            "WHERE e.EventType = 'PAYMENT' GROUP BY e.EventStatus").fetchall()))
        check("Payments with validity dates", len(payments), q(
            "SELECT COUNT(*) FROM dw.FactMemberActivity WHERE ValidFromDateKey IS NOT NULL AND ValidUntilDateKey IS NOT NULL").scalar())

        # Class enrollments
        check("Class enrollment rows", sum(len(set(t.get("userIds") or [])) for t in trainings), facts.get("CLASS_ENROLLMENT"))
        check("Enrollments per class", {t["idFunctionalTraining"]: len(set(t.get("userIds") or [])) for t in trainings if t.get("userIds")},
              dict(q("SELECT t.TrainingId, COUNT(*) FROM dw.FactMemberActivity f JOIN dw.DimTraining t ON t.TrainingKey = f.TrainingKey "
                     "WHERE f.TrainingKey > 0 GROUP BY t.TrainingId").fetchall()))

        # Churn predictions: every current MongoDB prediction must be stored in the history
        # under its calculation date, with the same risk level.
        stored = {(d, u): r for d, u, r in q(
            "SELECT SnapshotDate, UserIdMongo, RiskLevel FROM hist.ChurnPredictionSnapshot").fetchall()}
        expected_current = {(source_time(c["calculatedAt"], settings).date(), str(c["userId"])): c.get("riskLevel")
                            for c in churn if c.get("calculatedAt") and c.get("userId")}
        check("Current predictions stored in history", len(expected_current),
              sum(1 for k in expected_current if k in stored))
        check("Current predictions keep their risk level", 0,
              sum(1 for k, risk in expected_current.items() if k in stored and stored[k] != risk))
        check("Churn risk level distribution (current)", dict(Counter(expected_current.values())),
              dict(Counter(stored[k] for k in expected_current if k in stored)))
        check("Churn fact rows = history rows", q("SELECT COUNT(*) FROM hist.ChurnPredictionSnapshot").scalar(), facts.get("CHURN_PREDICTION"))
        check("Prediction confidence within 0..1", 0, q(
            "SELECT COUNT(*) FROM dw.FactMemberActivity WHERE PredictionConfidence < 0 OR PredictionConfidence > 1").scalar())

        # Integrity
        check("Fact rows mapped to unknown user (key 0)", 0, q("SELECT COUNT(*) FROM dw.FactMemberActivity WHERE UserKey = 0").scalar())
        check("Duplicated SourceEventId", 0, q(
            "SELECT COUNT(*) FROM (SELECT SourceEventId FROM dw.FactMemberActivity GROUP BY SourceEventId HAVING COUNT(*) > 1) d").scalar())
        last_status, last_finished = q("SELECT TOP 1 Status, FinishedAt FROM etl.LoadLog ORDER BY RunId DESC").fetchone() or (None, None)
        check("Last load status", "SUCCESS", last_status)
        now_local = datetime.now(settings.local_timezone).replace(tzinfo=None)
        check("Last load finished in the last 24 hours", True,
              last_finished is not None and now_local - last_finished < timedelta(hours=24))

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
    failed = sum(1 for _, ok, _ in checks if not ok)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
