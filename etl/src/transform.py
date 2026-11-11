"""Transform: build the star schema rows from the extracted MongoDB documents.

Every dimension gets deterministic surrogate keys (1..n, ordered by a natural
key) plus a key-0 member for "not applicable / unknown". The single fact table
holds four event types, told apart by DimEventType.
"""

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import holidays
from bson import DBRef

from config import Settings

DAY_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTH_NAMES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

EVENT_ATTENDANCE = "ATTENDANCE"
EVENT_PAYMENT = "PAYMENT"
EVENT_ENROLLMENT = "CLASS_ENROLLMENT"
EVENT_CHURN = "CHURN_PREDICTION"

NOT_APPLICABLE = "No aplica"
UNKNOWN = "Desconocido"

GROUP_CLASSES_PATTERN = re.compile(r"(\d+)\s+clases?\s+grupal", re.IGNORECASE)
INHERIT_PATTERN = re.compile(r"^todo de\s+(.+)$", re.IGNORECASE)

FACT_COLUMNS = [
    "EventDateKey", "EventTimeKey", "UserKey", "EventTypeKey", "PlanKey", "TrainingKey",
    "PaymentStatusKey", "ChurnRiskKey", "ValidFromDateKey", "ValidUntilDateKey",
    "ExternalInvoice", "TransactionId", "SourceEventId", "AttendanceMinutes", "PaymentAmount",
    "DaysSinceLastAttendance", "DaysSinceLastPayment", "PredictionConfidence",
    "ChurnReasons", "ChurnRecommendations",
]

SNAPSHOT_COLUMNS = [
    "SnapshotDate", "UserIdMongo", "CalculatedAt", "PlanName", "PlanLevel", "Profile", "NPayments",
    "DaysSinceLastPayment", "NAttendances", "AvgSessionMinutes", "DaysSinceLastAttendance",
    "ParticipatesInClasses", "Prediction", "PredictionConfidence", "RiskLevel", "RiskOrder",
    "Reasons", "Recommendations", "LoadedAt",
]


@dataclass
class Table:
    columns: list[str]
    rows: list[tuple] = field(default_factory=list)


@dataclass
class StarSchema:
    tables: dict[str, Table]
    snapshots_to_stage: Table
    expected_fact_counts: dict[str, int]
    source_counts: dict[str, int]
    orphan_rows: int
    warnings: list[str]


# Maximum length of every text column, mirroring sql/001_create_schema.sql.
STRING_LIMITS = {
    "DimDate": {"DayName": 20, "MonthName": 20},
    "DimTime": {"HourLabel": 10, "DayPart": 20},
    "DimEventType": {"EventType": 30, "EventStatus": 30},
    "DimUser": {"UserIdMongo": 24, "Identification": 20, "FullName": 130, "FirstName": 60, "LastName": 60, "Sex": 20,
                "AgeGroup": 20, "BloodType": 20, "Email": 150, "Phone": 20, "Role": 20},
    "DimPlan": {"PlanIdMongo": 24, "PlanName": 50, "Currency": 3, "Badge": 50, "Benefits": 1000},
    "DimTraining": {"TrainingName": 50, "Instructor": 100, "Description": 255, "Room": 50, "Status": 20},
    "DimPaymentStatus": {"Currency": 3, "ResponseCode": 10, "ResponseText": 50, "ResponseReason": 100,
                         "BankName": 50, "Franchise": 20},
    "DimChurnRisk": {"Prediction": 10, "RiskLevel": 20, "Profile": 50},
    "FactMemberActivity": {"ExternalInvoice": 50, "TransactionId": 50, "SourceEventId": 80,
                           "ChurnReasons": 1000, "ChurnRecommendations": 1000},
    "ChurnPredictionSnapshot": {"UserIdMongo": 24, "PlanName": 50, "Profile": 50, "Prediction": 10, "RiskLevel": 20,
                                "Reasons": 1000, "Recommendations": 1000},
}


def clip_strings(name: str, table: "Table", warnings: list[str]) -> None:
    """Truncate text that exceeds its column so one long value never aborts the load."""
    limits = {table.columns.index(c): n for c, n in STRING_LIMITS.get(name, {}).items() if c in table.columns}
    clipped = Counter()
    for i, row in enumerate(table.rows):
        if any(isinstance(row[j], str) and len(row[j]) > n for j, n in limits.items()):
            values = list(row)
            for j, n in limits.items():
                if isinstance(values[j], str) and len(values[j]) > n:
                    values[j] = values[j][:n]
                    clipped[table.columns[j]] += 1
            table.rows[i] = tuple(values)
    for column, count in clipped.items():
        warnings.append(f"{count} values truncated in {name}.{column} (max {STRING_LIMITS[name][column]} chars)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def local_time(value: datetime | None, settings: Settings) -> datetime | None:
    if value is None:
        return None
    if settings.source_timezone is None:
        return value
    return value.replace(tzinfo=timezone.utc).astimezone(settings.source_timezone).replace(tzinfo=None)


def date_key(value: date | datetime | None) -> int | None:
    if value is None:
        return None
    return value.year * 10000 + value.month * 100 + value.day


def ref_id(value) -> str | None:
    if isinstance(value, DBRef):
        return str(value.id)
    return str(value) if value is not None else None


def parse_iso(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def number(value):
    """Pass numbers through, turning NaN/inf (which SQL Server rejects) into None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_decimal(value, places: str) -> Decimal | None:
    value = number(value)
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def key_text(value) -> str | None:
    """Normalize natural keys to text so int/str mismatches never break lookups."""
    if value is None:
        return None
    return ref_id(value)


def joined(values) -> str | None:
    if not values:
        return None
    return " | ".join(str(v) for v in values)


def assign_keys(items: list, sort_key) -> dict:
    return {item: index for index, item in enumerate(sorted(items, key=sort_key), start=1)}


def age_on(birth: date | None, today: date) -> int | None:
    if birth is None:
        return None
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return age if age >= 0 else None


def age_group(age: int | None) -> str:
    if age is None:
        return "Sin dato"
    if age < 18:
        return "Menor de 18"
    for upper, label in ((24, "18-24"), (34, "25-34"), (44, "35-44"), (54, "45-54")):
        if age <= upper:
            return label
    return "55 o más"


def day_part(hour: int) -> str:
    if hour < 6:
        return "Madrugada"
    if hour < 12:
        return "Mañana"
    if hour < 18:
        return "Tarde"
    return "Noche"


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def expand_benefits(plan_name: str, benefits_by_plan: dict[str, list[str]], seen: set | None = None) -> list[str]:
    """Resolve "Todo de <Plan>" entries into that plan's benefits."""
    seen = (seen or set()) | {plan_name.lower()}
    expanded = []
    for benefit in benefits_by_plan.get(plan_name, []):
        match = INHERIT_PATTERN.match(benefit.strip())
        parent = next((p for p in benefits_by_plan if match and p.lower() == match.group(1).strip().lower()), None)
        if parent and parent.lower() not in seen:
            expanded.extend(expand_benefits(parent, benefits_by_plan, seen))
        else:
            expanded.append(benefit)
    return expanded


def weekly_group_classes(benefits: list[str]) -> int | None:
    """NULL means unlimited group classes."""
    if any("todas las clases grupales" in b.lower() for b in benefits):
        return None
    counts = [int(m.group(1)) for b in benefits if (m := GROUP_CLASSES_PATTERN.search(b))]
    return max(counts) if counts else 0


def build_dim_plan(plans: list[dict], churn: list[dict]) -> tuple[Table, dict, dict]:
    level_by_name = {c["plan"]: c["planLevel"] for c in churn if c.get("plan") and c.get("planLevel") is not None}
    price_rank = {p["planName"]: rank for rank, p in enumerate(sorted(plans, key=lambda p: p.get("price") or 0), start=1)}
    benefits_by_plan = {p["planName"]: p.get("benefits") or [] for p in plans}

    keys = assign_keys([str(p["_id"]) for p in plans], sort_key=lambda pid: next(
        (level_by_name.get(p["planName"], price_rank[p["planName"]]), p["planName"]) for p in plans if str(p["_id"]) == pid
    ))
    table = Table(["PlanKey", "PlanIdMongo", "PlanName", "PlanLevel", "Price", "Currency", "DurationDays", "Badge",
                   "Benefits", "WeeklyGroupClasses", "HasLockers", "CreatedDate", "UpdatedDate"])
    table.rows.append((0, "N/A", NOT_APPLICABLE, None, None, None, None, None, None, None, None, None, None))
    for plan in plans:
        name = plan["planName"]
        expanded = expand_benefits(name, benefits_by_plan)
        table.rows.append((
            keys[str(plan["_id"])], str(plan["_id"]), name, number(level_by_name.get(name, price_rank[name])),
            to_decimal(plan.get("price"), "0.01"), plan.get("currency"), number(plan.get("durationDays")), plan.get("badge"),
            "; ".join(plan.get("benefits") or []) or None, weekly_group_classes(expanded),
            any("casillero" in b.lower() for b in expanded), parse_iso(plan.get("createdAt")), parse_iso(plan.get("updatedAt")),
        ))
    by_id = {str(p["_id"]): keys[str(p["_id"])] for p in plans}
    by_name = {p["planName"]: keys[str(p["_id"])] for p in plans}
    return table, by_id, by_name


def build_dim_user(users: list[dict], today: date, settings: Settings) -> tuple[Table, dict, dict]:
    keys = assign_keys([str(u["_id"]) for u in users], sort_key=lambda uid: uid)
    table = Table(["UserKey", "UserIdMongo", "Identification", "FullName", "FirstName", "LastName", "Sex", "BirthDate",
                   "Age", "AgeGroup", "BloodType", "Email", "Phone", "Role", "IsActive", "RegistrationDate", "UpdatedDate"])
    table.rows.append((0, "N/A", None, UNKNOWN, None, None, None, None, None, None, None, None, None, None, None, None, None))
    for user in users:
        birth = user["birthDate"].date() if user.get("birthDate") else None
        age = age_on(birth, today)
        first, last = user.get("name"), user.get("lastname")
        identification = user.get("identification")
        table.rows.append((
            keys[str(user["_id"])], str(user["_id"]), str(identification) if identification is not None else None,
            " ".join(p for p in (first, last) if p) or UNKNOWN, first, last, user.get("sex"), birth, age, age_group(age),
            user.get("bloodType"), user.get("email"), user.get("phone"), user.get("role"), user.get("isActive"),
            local_time(user.get("createdAt"), settings), local_time(user.get("updatedAt"), settings),
        ))
    by_id = {str(u["_id"]): keys[str(u["_id"])] for u in users}
    by_identification = {key_text(u["identification"]): keys[str(u["_id"])] for u in users if u.get("identification") is not None}
    return table, by_id, by_identification


def build_dim_training(trainings: list[dict], settings: Settings) -> tuple[Table, dict]:
    keys = assign_keys([str(t["_id"]) for t in trainings], sort_key=lambda tid: next(
        (t.get("idFunctionalTraining") or 0, tid) for t in trainings if str(t["_id"]) == tid
    ))
    table = Table(["TrainingKey", "TrainingId", "TrainingName", "Instructor", "Description", "Room", "MaximumCapacity",
                   "DurationMinutes", "Status", "CreatedDate"])
    table.rows.append((0, None, NOT_APPLICABLE, None, None, None, None, None, None, None))
    for training in trainings:
        duration = str(training.get("duration") or "").strip()
        table.rows.append((
            keys[str(training["_id"])], training.get("idFunctionalTraining"), training.get("nameTraining") or UNKNOWN,
            training.get("instructorName") or training.get("instructor"), training.get("description"), training.get("room"),
            number(training.get("maximumCapacity")), int(duration) if duration.isdigit() else None, training.get("status"),
            local_time(training.get("createdAt"), settings),
        ))
    return table, {str(t["_id"]): keys[str(t["_id"])] for t in trainings}


def payment_status_tuple(payment: dict) -> tuple:
    return tuple(payment.get(f) for f in ("currency", "responseCode", "responseText", "responseReason", "bankName", "franchise"))


def build_dim_payment_status(payments: list[dict]) -> tuple[Table, dict]:
    combos = {payment_status_tuple(p) for p in payments}
    keys = assign_keys(list(combos), sort_key=lambda c: tuple("" if v is None else str(v) for v in c))
    table = Table(["PaymentStatusKey", "Currency", "ResponseCode", "ResponseText", "ResponseReason", "BankName", "Franchise"])
    table.rows.append((0, None, None, NOT_APPLICABLE, None, None, None))
    table.rows.extend((key, *combo) for combo, key in sorted(keys.items(), key=lambda kv: kv[1]))
    return table, keys


def churn_risk_tuple(snapshot: dict) -> tuple:
    return tuple(snapshot.get(f) for f in ("Prediction", "RiskLevel", "RiskOrder", "Profile", "ParticipatesInClasses"))


def build_dim_churn_risk(snapshots: list[dict]) -> tuple[Table, dict]:
    combos = {churn_risk_tuple(s) for s in snapshots}
    keys = assign_keys(list(combos), sort_key=lambda c: tuple("" if v is None else str(v) for v in c))
    table = Table(["ChurnRiskKey", "Prediction", "RiskLevel", "RiskOrder", "Profile", "ParticipatesInClasses"])
    table.rows.append((0, NOT_APPLICABLE, None, None, None, None))
    table.rows.extend((key, *combo) for combo, key in sorted(keys.items(), key=lambda kv: kv[1]))
    return table, keys


def build_dim_event_type(pairs: set[tuple[str, str]]) -> tuple[Table, dict]:
    keys = assign_keys(list(pairs), sort_key=lambda p: p)
    table = Table(["EventTypeKey", "EventType", "EventStatus"])
    table.rows.extend((key, *pair) for pair, key in sorted(keys.items(), key=lambda kv: kv[1]))
    return table, keys


def build_dim_time(attendance_hours: list[int]) -> Table:
    """An hour is peak when its check-in count is in the top quartile of active hours."""
    counts = {hour: attendance_hours.count(hour) for hour in range(24)}
    active = sorted(c for c in counts.values() if c > 0)
    threshold = statistics.quantiles(active, n=4, method="inclusive")[2] if len(active) >= 2 else (active[0] if active else 1)
    table = Table(["TimeKey", "HourNumber", "HourLabel", "DayPart", "IsPeakHour"])
    for hour in range(24):
        table.rows.append((hour, hour, f"{hour:02d}:00", day_part(hour), counts[hour] > 0 and counts[hour] >= threshold))
    return table


def build_dim_date(used_keys: set[int], today: date) -> Table:
    first = date(min(used_keys) // 10000, 1, 1)
    last = date(max(max(used_keys) // 10000, today.year + 1), 12, 31)
    co_holidays = holidays.country_holidays("CO", years=range(first.year, last.year + 1))
    table = Table(["DateKey", "FullDate", "DayOfMonth", "DayOfWeekNumber", "DayName", "WeekNumber", "MonthNumber",
                   "MonthName", "YearMonth", "QuarterNumber", "YearNumber", "IsWeekend", "IsHoliday"])
    day = first
    while day <= last:
        weekday = day.isoweekday()
        table.rows.append((
            date_key(day), day, day.day, weekday, DAY_NAMES[weekday - 1], day.isocalendar().week, day.month,
            MONTH_NAMES[day.month - 1], day.year * 100 + day.month, (day.month - 1) // 3 + 1, day.year,
            weekday >= 6, day in co_holidays,
        ))
        day += timedelta(days=1)
    return table


# ---------------------------------------------------------------------------
# Churn history
# ---------------------------------------------------------------------------

def build_current_snapshots(churn: list[dict], settings: Settings, loaded_at: datetime) -> list[dict]:
    """One snapshot per (calculation date, user); the latest calculation wins if the source repeats a user."""
    snapshots: dict[tuple, dict] = {}
    for c in churn:
        calculated = local_time(c.get("calculatedAt"), settings)
        if calculated is None or not c.get("userId"):
            continue
        confidence = number(c.get("predictionConfidence"))
        snapshot = {
            "SnapshotDate": calculated.date(),
            "UserIdMongo": key_text(c["userId"]),
            "CalculatedAt": calculated,
            "PlanName": c.get("plan"),
            "PlanLevel": number(c.get("planLevel")),
            "Profile": c.get("profile"),
            "NPayments": number(c.get("nPayments")),
            "DaysSinceLastPayment": number(c.get("daysSinceLastPayment")),
            "NAttendances": number(c.get("nAttendances")),
            "AvgSessionMinutes": to_decimal(c.get("avgSessionMinutes"), "0.01"),
            "DaysSinceLastAttendance": number(c.get("daysSinceLastAtt")),
            "ParticipatesInClasses": c.get("participatesInClasses"),
            "Prediction": c.get("prediction"),
            # Source stores a percentage (0-100); the warehouse stores a ratio (0-1).
            "PredictionConfidence": to_decimal(confidence / 100, "0.0001") if confidence is not None else None,
            "RiskLevel": c.get("riskLevel"),
            "RiskOrder": number(c.get("riskOrder")),
            "Reasons": joined(c.get("reasons")),
            "Recommendations": joined(c.get("recommendations")),
            "LoadedAt": loaded_at,
        }
        key = (snapshot["SnapshotDate"], snapshot["UserIdMongo"])
        if key not in snapshots or snapshots[key]["CalculatedAt"] <= calculated:
            snapshots[key] = snapshot
    return [snapshots[k] for k in sorted(snapshots)]


def merge_snapshots(existing: list[dict], current: list[dict]) -> list[dict]:
    """Same day and user: the current run replaces the stored snapshot."""
    merged = {(s["SnapshotDate"], s["UserIdMongo"]): s for s in existing}
    merged.update({(s["SnapshotDate"], s["UserIdMongo"]): s for s in current})
    return [merged[k] for k in sorted(merged)]


# ---------------------------------------------------------------------------
# Star schema
# ---------------------------------------------------------------------------

def build_star_schema(data: dict[str, list[dict]], history: list[dict], settings: Settings) -> StarSchema:
    loaded_at = datetime.now(settings.local_timezone).replace(tzinfo=None, microsecond=0)
    today = loaded_at.date()
    warnings: list[str] = []

    users, plans, payments = data["users"], data["plans"], data["payments"]
    attendances, trainings, churn = data["attendances"], data["functionalTrainings"], data["churnPredictions"]

    dim_user, user_by_id, user_by_identification = build_dim_user(users, today, settings)
    dim_plan, plan_by_id, plan_by_name = build_dim_plan(plans, churn)
    dim_training, training_by_id = build_dim_training(trainings, settings)
    dim_payment_status, payment_status_keys = build_dim_payment_status(payments)

    current_snapshots = build_current_snapshots(churn, settings, loaded_at)
    all_snapshots = merge_snapshots(history, current_snapshots)
    dim_churn_risk, churn_risk_keys = build_dim_churn_risk(all_snapshots)

    event_pairs = (
        {(EVENT_ATTENDANCE, a.get("result") or UNKNOWN) for a in attendances}
        | {(EVENT_PAYMENT, p.get("status") or UNKNOWN) for p in payments}
        | {(EVENT_ENROLLMENT, t.get("status") or UNKNOWN) for t in trainings if t.get("userIds")}
        | {(EVENT_CHURN, s.get("Prediction") or UNKNOWN) for s in all_snapshots}
    )
    dim_event_type, event_keys = build_dim_event_type(event_pairs)

    orphans: dict[str, int] = {}

    def lookup(mapping: dict, key, label: str) -> int:
        if key in mapping:
            return mapping[key]
        orphans[label] = orphans.get(label, 0) + 1
        return 0

    facts: list[tuple] = []
    attendance_hours: list[int] = []

    for a in attendances:
        check_in, check_out = local_time(a.get("checkIn"), settings), local_time(a.get("checkOut"), settings)
        if check_in is None:
            warnings.append(f"Attendance {a['_id']} skipped: no checkIn")
            continue
        attendance_hours.append(check_in.hour)
        minutes = round((check_out - check_in).total_seconds() / 60) if check_out else None
        facts.append(dict(
            EventDateKey=date_key(check_in), EventTimeKey=check_in.hour,
            UserKey=lookup(user_by_id, ref_id(a.get("user")) or key_text(a.get("userId")), "attendance.user"),
            EventTypeKey=event_keys[(EVENT_ATTENDANCE, a.get("result") or UNKNOWN)],
            PlanKey=lookup(plan_by_name, a.get("userPlan"), "attendance.userPlan") if a.get("userPlan") else 0,
            SourceEventId=f"ATT-{a['_id']}", AttendanceMinutes=minutes,
        ))

    for p in payments:
        happened = local_time(p.get("transactionDate") or p.get("createdAt"), settings)
        if happened is None:
            warnings.append(f"Payment {p['_id']} skipped: no transactionDate/createdAt")
            continue
        facts.append(dict(
            EventDateKey=date_key(happened), EventTimeKey=happened.hour,
            UserKey=lookup(user_by_id, ref_id(p.get("user")) or key_text(p.get("userId")), "payment.user"),
            EventTypeKey=event_keys[(EVENT_PAYMENT, p.get("status") or UNKNOWN)],
            PlanKey=lookup(plan_by_id, ref_id(p.get("plan")), "payment.plan"),
            PaymentStatusKey=payment_status_keys[payment_status_tuple(p)],
            ValidFromDateKey=date_key(local_time(p.get("validFrom"), settings)),
            ValidUntilDateKey=date_key(local_time(p.get("validUntil"), settings)),
            ExternalInvoice=p.get("externalInvoice"), TransactionId=p.get("transactionId"),
            SourceEventId=f"PAY-{p['_id']}", PaymentAmount=to_decimal(p.get("amount"), "0.01"),
        ))

    enrollments = 0
    for t in trainings:
        scheduled = local_time(t.get("datetime"), settings)
        identifications = t.get("userIds") or []
        if scheduled is None:
            if identifications:
                warnings.append(f"Training {t['_id']} skipped: no datetime")
            continue
        for identification in dict.fromkeys(key_text(i) for i in identifications if i is not None):
            enrollments += 1
            facts.append(dict(
                EventDateKey=date_key(scheduled), EventTimeKey=scheduled.hour,
                UserKey=lookup(user_by_identification, identification, "enrollment.userIds"),
                EventTypeKey=event_keys[(EVENT_ENROLLMENT, t.get("status") or UNKNOWN)],
                TrainingKey=training_by_id[str(t["_id"])],
                SourceEventId=f"ENR-{t['_id']}-{identification}",
            ))

    for s in all_snapshots:
        facts.append(dict(
            EventDateKey=date_key(s["SnapshotDate"]), EventTimeKey=s["CalculatedAt"].hour,
            UserKey=lookup(user_by_id, s["UserIdMongo"], "churn.userId"),
            EventTypeKey=event_keys[(EVENT_CHURN, s.get("Prediction") or UNKNOWN)],
            PlanKey=lookup(plan_by_name, s.get("PlanName"), "churn.plan") if s.get("PlanName") else 0,
            ChurnRiskKey=churn_risk_keys[churn_risk_tuple(s)],
            SourceEventId=f"CHU-{s['UserIdMongo']}-{date_key(s['SnapshotDate'])}",
            DaysSinceLastAttendance=s.get("DaysSinceLastAttendance"), DaysSinceLastPayment=s.get("DaysSinceLastPayment"),
            PredictionConfidence=s.get("PredictionConfidence"),
            ChurnReasons=s.get("Reasons"), ChurnRecommendations=s.get("Recommendations"),
        ))

    for label, count in orphans.items():
        warnings.append(f"{count} fact rows reference a missing {label}; mapped to key 0")

    defaults = {"PlanKey": 0, "TrainingKey": 0, "PaymentStatusKey": 0, "ChurnRiskKey": 0}
    fact_rows = [tuple({**defaults, **f}.get(column) for column in FACT_COLUMNS) for f in facts]

    used_date_keys = {f[k] for f in facts for k in ("EventDateKey", "ValidFromDateKey", "ValidUntilDateKey") if f.get(k)}
    dim_date = build_dim_date(used_date_keys or {date_key(today)}, today)
    dim_time = build_dim_time(attendance_hours)

    snapshots_to_stage = Table(SNAPSHOT_COLUMNS, [tuple(s[c] for c in SNAPSHOT_COLUMNS) for s in current_snapshots])

    tables = {
        "DimDate": dim_date,
        "DimTime": dim_time,
        "DimEventType": dim_event_type,
        "DimUser": dim_user,
        "DimPlan": dim_plan,
        "DimTraining": dim_training,
        "DimPaymentStatus": dim_payment_status,
        "DimChurnRisk": dim_churn_risk,
        "FactMemberActivity": Table(FACT_COLUMNS, fact_rows),
    }
    for name, table in tables.items():
        clip_strings(name, table, warnings)
    clip_strings("ChurnPredictionSnapshot", snapshots_to_stage, warnings)

    return StarSchema(
        tables=tables,
        snapshots_to_stage=snapshots_to_stage,
        expected_fact_counts={
            EVENT_ATTENDANCE: sum(1 for a in attendances if a.get("checkIn")),
            EVENT_PAYMENT: sum(1 for p in payments if p.get("transactionDate") or p.get("createdAt")),
            EVENT_ENROLLMENT: enrollments,
            EVENT_CHURN: len(all_snapshots),
        },
        source_counts={name: len(docs) for name, docs in data.items()},
        orphan_rows=sum(orphans.values()),
        warnings=warnings,
    )
