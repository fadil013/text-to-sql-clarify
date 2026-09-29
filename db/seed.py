"""Deterministic synthetic data for the demo shop, including deliberately messy cases.

Run:  python -m db.seed            (needs the DB from `docker compose up -d`)
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import psycopg
from faker import Faker

from app.config import get_settings

SEED = 42
N_CUSTOMERS = 600
N_DUPLICATES = 25
N_TEST_ACCOUNTS = 10
HISTORY_DAYS = 730

CATEGORIES = {
    "Electronics": [("Wireless Headphones", 8999), ("USB-C Hub", 3499), ("Smart Watch", 19999),
                    ("Bluetooth Speaker", 5999), ("Webcam HD", 4999), ("Mechanical Keyboard", 10999)],
    "Home": [("Desk Lamp", 2999), ("Coffee Grinder", 4599), ("Air Purifier", 12999),
             ("Throw Blanket", 3999), ("Storage Bins", 2499), ("Wall Clock", 1999)],
    "Fitness": [("Yoga Mat", 2999), ("Resistance Bands", 1999), ("Water Bottle", 1799),
                ("Dumbbell Set", 8999), ("Jump Rope", 1299), ("Foam Roller", 2499)],
    "Office": [("Notebook Pack", 1299), ("Ergonomic Chair", 24999), ("Standing Desk", 39999),
               ("Monitor Arm", 7999), ("Desk Organizer", 1999), ("Whiteboard", 3499)],
    "Software": [("Photo Editor License", 5999), ("VPN 1-Year", 4999), ("Antivirus Suite", 3999),
                 ("Cloud Backup 1TB", 6999), ("Password Manager", 2999), ("Video Editor License", 9999)],
}
PLANS = {"basic": 900, "pro": 2900, "enterprise": 9900}
COUNTRIES = ["US"] * 40 + ["GB"] * 12 + ["DE"] * 10 + ["FR"] * 8 + ["CA"] * 8 + ["AU"] * 6 + \
            ["IN"] * 6 + ["BR"] * 4 + ["JP"] * 3 + ["NL"] * 3
REFUND_REASONS = ["damaged", "not as described", "changed mind", "late delivery", None]


def _dt(anchor: datetime, days_ago: float) -> datetime:
    return anchor - timedelta(days=days_ago)


def generate_dataset(anchor: datetime | None = None) -> dict[str, list[tuple]]:
    """Pure function: same anchor + seed -> identical rows. Rows are tuples in column order."""
    anchor = anchor or datetime.now(timezone.utc).replace(microsecond=0)
    rng = random.Random(SEED)
    fake = Faker("en_US")
    Faker.seed(SEED)

    # ---- products
    products = []
    pid = 0
    for cat, items in CATEGORIES.items():
        for name, price in items:
            pid += 1
            products.append((pid, name, cat, price, rng.random() > 0.08))
    price_of = {p[0]: p[3] for p in products}

    # ---- customers
    customers = []
    for cid in range(1, N_CUSTOMERS + 1):
        name = fake.name()
        email = f"{name.lower().replace(' ', '.').replace('..', '.')}{rng.randint(1, 99)}@{fake.free_email_domain()}"
        phone = fake.phone_number() if rng.random() > 0.2 else None
        country = rng.choice(COUNTRIES) if rng.random() > 0.05 else None
        created = _dt(anchor, rng.uniform(5, HISTORY_DAYS))
        customers.append([cid, name, email, phone, country, rng.random() < 0.4, False, created])

    # near-duplicate customers: same person, tweaked name/email, signed up later
    for i in range(N_DUPLICATES):
        src = customers[rng.randrange(0, N_CUSTOMERS)]
        cid = len(customers) + 1
        first, _, last = src[1].partition(" ")
        variant = rng.choice([f"{first[0]}. {last}", src[1].upper(), f"{first} {last}  "])
        email = src[2].replace("@", f"{rng.randint(1, 9)}@", 1)
        created = min(anchor - timedelta(days=1), src[7] + timedelta(days=rng.uniform(10, 200)))
        customers.append([cid, variant, email, src[3], src[4], src[5], False, created])

    # test accounts (should usually be excluded from business metrics)
    for i in range(N_TEST_ACCOUNTS):
        cid = len(customers) + 1
        customers.append([cid, f"QA Tester {i + 1}", f"qa{i + 1}@example.test", None, "US", False, True,
                          _dt(anchor, rng.uniform(30, HISTORY_DAYS))])
    customers = [tuple(c) for c in customers]

    # ---- orders / items / payments / refunds
    orders, items, payments, refunds = [], [], [], []
    oid = iid = payid = rid = 0
    # heavy-tailed activity: a few whales, many one-timers
    for c in customers:
        cid, created = c[0], c[7]
        r = rng.random()
        n_orders = 0 if r < 0.12 else 1 if r < 0.45 else rng.randint(2, 4) if r < 0.85 \
            else rng.randint(5, 14) if r < 0.97 else rng.randint(15, 30)
        currency = rng.choices(["USD", "EUR", "GBP"], [0.85, 0.10, 0.05])[0]
        span = max((anchor - created).days, 1)
        for _ in range(n_orders):
            oid += 1
            o_created = created + timedelta(days=rng.uniform(0, span), hours=rng.uniform(0, 23))
            o_created = min(o_created, anchor - timedelta(hours=1))
            age_days = (anchor - o_created).days
            status = rng.choices(["delivered", "shipped", "paid", "pending", "cancelled"],
                                 [0.62, 0.06, 0.07, 0.05, 0.20])[0]
            if age_days > 14 and status in ("shipped", "paid"):
                status = "delivered"
            if age_days > 30 and status == "pending":
                status = "cancelled"

            order_lines = []
            for _ in range(rng.choices([1, 2, 3, 4], [0.55, 0.28, 0.12, 0.05])[0]):
                p = rng.randint(1, len(products))
                drift = rng.choice([1.0, 1.0, 1.0, 0.9, 1.1])  # price at time of sale differs a bit
                order_lines.append((p, rng.choices([1, 2, 3], [0.8, 0.15, 0.05])[0],
                                    int(price_of[p] * drift)))
            total = sum(q * u for _, q, u in order_lines)
            for p, q, u in order_lines:
                iid += 1
                items.append((iid, oid, p, q, u))
            orders.append((oid, cid, status, currency, None if rng.random() < 0.02 else total, o_created))

            # payments
            if status == "cancelled":
                if rng.random() < 0.4:
                    payid += 1
                    payments.append((payid, oid, total, rng.choice(["card", "paypal"]), "failed",
                                     None, None))
                continue
            if status == "pending":
                payid += 1
                payments.append((payid, oid, total, "bank_transfer", "pending", None, None))
                continue
            if rng.random() < 0.08:  # a failed attempt before the successful one
                payid += 1
                payments.append((payid, oid, total, "card", "failed", str(rng.randint(1000, 9999)), None))
            payid += 1
            method = rng.choices(["card", "paypal", "bank_transfer"], [0.7, 0.2, 0.1])[0]
            paid_at = o_created + timedelta(minutes=rng.randint(1, 90))
            payments.append((payid, oid, total, method, "succeeded",
                             str(rng.randint(1000, 9999)) if method == "card" else None, paid_at))
            if rng.random() < 0.08:  # refund, full or partial
                rid += 1
                amount = total if rng.random() < 0.5 else max(1, int(total * rng.uniform(0.2, 0.8)))
                refunds.append((rid, payid, amount, rng.choice(REFUND_REASONS),
                                min(anchor, paid_at + timedelta(days=rng.uniform(1, 20)))))
    # ---- subscriptions
    subs = []
    sid = 0
    for c in customers:
        if c[6] or rng.random() > 0.30:
            continue
        sid += 1
        plan = rng.choices(list(PLANS), [0.5, 0.35, 0.15])[0]
        started = min(anchor - timedelta(days=2), c[7] + timedelta(days=rng.uniform(0, 120)))
        status = rng.choices(["active", "past_due", "cancelled"], [0.6, 0.08, 0.32])[0]
        cancelled_at = None
        if status == "cancelled":
            cancelled_at = min(anchor, started + timedelta(days=rng.uniform(20, 300)))
        subs.append((sid, c[0], plan, status, PLANS[plan], started, cancelled_at))

    return {"customers": customers, "products": products, "orders": orders,
            "order_items": items, "payments": payments, "refunds": refunds, "subscriptions": subs}


_COLUMNS = {
    "customers": "id, full_name, email, phone, country, marketing_opt_in, is_test_account, created_at",
    "products": "id, name, category, price_cents, is_active",
    "orders": "id, customer_id, status, currency, total_cents, created_at",
    "order_items": "id, order_id, product_id, quantity, unit_price_cents",
    "payments": "id, order_id, amount_cents, method, status, card_last4, paid_at",
    "refunds": "id, payment_id, amount_cents, reason, created_at",
    "subscriptions": "id, customer_id, plan, status, monthly_price_cents, started_at, cancelled_at",
}
LOAD_ORDER = ["customers", "products", "orders", "order_items", "payments", "refunds", "subscriptions"]


def load(dataset: dict[str, list[tuple]] | None = None) -> dict[str, int]:
    dataset = dataset or generate_dataset()
    counts = {}
    with psycopg.connect(get_settings().admin_dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(LOAD_ORDER) + " RESTART IDENTITY CASCADE")
        for table in LOAD_ORDER:
            cols = _COLUMNS[table]
            ph = ", ".join(["%s"] * len(cols.split(",")))
            cur.executemany(f"INSERT INTO {table} ({cols}) VALUES ({ph})", dataset[table])
            counts[table] = len(dataset[table])
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"COALESCE(MAX(id), 1)) FROM {table}")
    return counts


if __name__ == "__main__":
    for table, n in load().items():
        print(f"{table:15s} {n}")
