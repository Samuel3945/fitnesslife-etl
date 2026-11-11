"""Read-only profiling of the Fitness Life MongoDB source.

Prints a Markdown report with document counts, field presence and types,
categorical distributions, numeric/date ranges and referential integrity
checks. Personal data is never printed: sensitive fields only report
presence and type.

Usage:
    python src/profile_source.py > report.md
"""

import os
from collections import Counter, defaultdict
from datetime import datetime

from bson import DBRef, ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

ANALYTIC_COLLECTIONS = [
    "users",
    "plans",
    "payments",
    "attendances",
    "functionalTrainings",
    "churnPredictions",
]

SENSITIVE_FIELDS = {
    "password",
    "email",
    "userEmail",
    "phone",
    "identification",
    "name",
    "lastname",
    "userName",
    "userPhoto",
    "qrCode",
    "signature",
    "approvalCode",
    "transactionId",
    "externalInvoice",
    "userIds",
    "userId",
    "user",
    "plan",
    "birthDate",
}

CATEGORICAL_FIELDS = {
    "users": ["sex", "bloodType", "role", "isActive", "_class"],
    "plans": ["planName", "price", "currency", "durationDays", "badge", "benefits", "createdAt", "updatedAt"],
    "payments": ["currency", "status", "bankName", "franchise", "responseCode", "responseText", "responseReason", "planName"],
    "attendances": ["result", "userPlan", "_class"],
    "functionalTrainings": ["nameTraining", "instructor", "duration", "status", "room", "maximumCapacity"],
    "churnPredictions": ["plan", "planLevel", "profile", "prediction", "riskLevel", "riskOrder", "participatesInClasses", "reasons", "recommendations"],
}

TOP_N = 25


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, DBRef):
        return "DBRef"
    if isinstance(value, ObjectId):
        return "ObjectId"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, datetime):
        return "date"
    if isinstance(value, list):
        inner = {type_name(v) for v in value}
        return f"array<{'|'.join(sorted(inner)) or 'empty'}>"
    return type(value).__name__


def ref_id(value):
    if isinstance(value, DBRef):
        return str(value.id)
    if value is None:
        return None
    return str(value)


def profile_collection(db, name):
    docs = list(db[name].find({}, {"password": 0}))
    total = len(docs)
    lines = [f"## `{name}` — {total} documentos", ""]
    if total == 0:
        return lines + ["_Colección vacía._", ""], docs

    presence = Counter()
    types = defaultdict(Counter)
    numeric = defaultdict(list)
    dates = defaultdict(list)
    for doc in docs:
        for field, value in doc.items():
            presence[field] += 1
            types[field][type_name(value)] += 1
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numeric[field].append(value)
            elif isinstance(value, datetime):
                dates[field].append(value)

    lines += ["| Campo | Presente | Tipos | Rango |", "|---|---|---|---|"]
    for field in sorted(presence, key=lambda f: (-presence[f], f)):
        pct = presence[field] * 100 / total
        type_str = ", ".join(f"{t} ({c})" for t, c in types[field].most_common())
        if field in numeric and field not in SENSITIVE_FIELDS:
            values = numeric[field]
            range_str = f"{min(values)} … {max(values)}"
        elif field in dates and field not in SENSITIVE_FIELDS:
            values = dates[field]
            range_str = f"{min(values):%Y-%m-%d %H:%M} … {max(values):%Y-%m-%d %H:%M} UTC"
        else:
            range_str = ""
        marker = " 🔒" if field in SENSITIVE_FIELDS else ""
        lines.append(f"| `{field}`{marker} | {pct:.1f}% | {type_str} | {range_str} |")
    lines.append("")

    for field in CATEGORICAL_FIELDS.get(name, []):
        counter = Counter()
        missing = 0
        for doc in docs:
            if field not in doc or doc[field] is None:
                missing += 1
                continue
            value = doc[field]
            items = value if isinstance(value, list) else [value]
            for item in items:
                counter[repr(item) if not isinstance(item, str) else item] += 1
        lines.append(f"**`{field}`** — {len(counter)} valores distintos, {missing} faltantes")
        lines.append("")
        for value, count in counter.most_common(TOP_N):
            lines.append(f"- `{value}` → {count}")
        if len(counter) > TOP_N:
            lines.append(f"- … y {len(counter) - TOP_N} más")
        lines.append("")

    return lines, docs


def integrity_checks(data):
    users = data["users"]
    user_ids = {str(u["_id"]) for u in users}
    user_identifications = {u.get("identification") for u in users if u.get("identification") is not None}
    plan_ids = {str(p["_id"]) for p in data["plans"]}
    plan_names = {p.get("planName") for p in data["plans"]}

    checks = []

    def check(label, values, valid):
        values = [v for v in values if v is not None]
        orphans = [v for v in values if v not in valid]
        checks.append(f"| {label} | {len(values)} | {len(orphans)} |")

    check("attendances.user → users._id", [ref_id(a.get("user")) for a in data["attendances"]], user_ids)
    check("attendances.userId → users._id", [a.get("userId") for a in data["attendances"]], user_ids)
    mismatched = sum(
        1
        for a in data["attendances"]
        if a.get("userId") and a.get("user") and a.get("userId") != ref_id(a.get("user"))
    )
    checks.append(f"| attendances.userId ≠ attendances.user | {len(data['attendances'])} | {mismatched} |")
    check("payments.user → users._id", [ref_id(p.get("user")) for p in data["payments"]], user_ids)
    check("payments.plan → plans._id", [ref_id(p.get("plan")) for p in data["payments"]], plan_ids)
    check("payments.planName → plans.planName", [p.get("planName") for p in data["payments"]], plan_names)
    check("attendances.userPlan → plans.planName", [a.get("userPlan") for a in data["attendances"]], plan_names)
    check(
        "functionalTrainings.userIds → users.identification",
        [uid for t in data["functionalTrainings"] for uid in (t.get("userIds") or [])],
        user_identifications,
    )
    check("churnPredictions.userId → users._id", [c.get("userId") for c in data["churnPredictions"]], user_ids)
    check("churnPredictions.plan → plans.planName", [c.get("plan") for c in data["churnPredictions"]], plan_names)

    no_checkout = sum(1 for a in data["attendances"] if not a.get("checkOut"))
    negative = sum(
        1
        for a in data["attendances"]
        if a.get("checkIn") and a.get("checkOut") and a["checkOut"] < a["checkIn"]
    )
    checks.append(f"| attendances sin checkOut | {len(data['attendances'])} | {no_checkout} |")
    checks.append(f"| attendances con checkOut < checkIn | {len(data['attendances'])} | {negative} |")

    duplicated = sum(c - 1 for c in Counter(c.get("userId") for c in data["churnPredictions"]).values() if c > 1)
    checks.append(f"| churnPredictions repetidas por userId | {len(data['churnPredictions'])} | {duplicated} |")

    return ["## Integridad referencial", "", "| Chequeo | Revisados | Problemas |", "|---|---|---|", *checks, ""]


def main():
    load_dotenv()
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=20000)
    db = client[os.environ.get("MONGODB_DB", "fitnesslife")]

    report = [
        "# Perfilado de MongoDB — Fitness Life",
        "",
        f"Generado: {datetime.now():%Y-%m-%d %H:%M}. Campos marcados con 🔒 son sensibles: solo se reporta presencia y tipo.",
        "",
        "## Colecciones en la base",
        "",
        "| Colección | Documentos | ¿Analítica? |",
        "|---|---|---|",
    ]
    for name in sorted(db.list_collection_names()):
        analytic = "sí" if name in ANALYTIC_COLLECTIONS else "no"
        report.append(f"| `{name}` | {db[name].estimated_document_count()} | {analytic} |")
    report.append("")

    data = {}
    for name in ANALYTIC_COLLECTIONS:
        lines, docs = profile_collection(db, name)
        report += lines
        data[name] = docs

    report += integrity_checks(data)
    print("\n".join(report))
    client.close()


if __name__ == "__main__":
    main()
