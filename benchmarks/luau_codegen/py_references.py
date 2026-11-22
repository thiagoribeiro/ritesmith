"""Hand-written Python (Monty subset) solutions for a sample of tasks, one or more per
tier. selftest.py runs them to prove the python target's harness — type stubs, tool
bridge, grading — agrees with the Luau references."""

PY_REFERENCES: dict[str, str] = {
    "t1_slugify": '''
import re


def run(input: Input, context: Context) -> Output:
    slug = re.sub(r"[^a-z0-9]+", "-", input["text"].lower())
    return {"slug": slug.strip("-")}
''',
    "t2_price_label": '''
def run(input: Input, context: Context) -> Output:
    r = tools.market.get_price({"symbol": input["symbol"], "currency": input["currency"]})
    if not r["ok"]:
        return {"error": r["error"], "message": r["message"]}
    price = round(r["price"], 2)
    return {
        "symbol": r["symbol"],
        "currency": r["currency"],
        "price": price,
        "label": f"{r['symbol']}: {price:.2f} {r['currency']}",
    }
''',
    "t3_notify_inactive": '''
def run(input: Input, context: Context) -> Output:
    r = tools.crm.list({"segment": input["segment"]})
    if not r["ok"]:
        return {"error": r["error"], "message": r["message"]}
    notified: list[str] = []
    failed: list[str] = []
    for c in r["customers"]:
        if c["last_seen_days"] >= input["inactive_days"]:
            sent = tools.message.send({"user": c["id"], "text": f"Hi {c['name']}, we miss you!"})
            if sent["ok"]:
                notified.append(c["id"])
            else:
                failed.append(c["id"])
    return {"notified": notified, "failed": failed, "count": len(notified)}
''',
    "t3_order_fulfillment": '''
def first_shortage(order: Order) -> str | None:
    for item in order["items"]:
        stock = tools.inventory.check({"sku": item["sku"]})
        if not stock["ok"] or stock["available"] < item["qty"]:
            return item["sku"]
    return None


def run(input: Input, context: Context) -> Output:
    shipped: list[Shipped] = []
    blocked: list[Blocked] = []
    for oid in input["order_ids"]:
        r = tools.orders.get({"id": oid})
        if not r["ok"]:
            blocked.append({"order_id": oid, "reason": "not_found"})
        elif r["order"]["status"] != "paid":
            blocked.append({"order_id": oid, "reason": "not_paid"})
        else:
            sku = first_shortage(r["order"])
            if sku is not None:
                blocked.append({"order_id": oid, "reason": "out_of_stock:" + sku})
            else:
                s = tools.shipping.create({"order_id": oid})
                if s["ok"]:
                    shipped.append({"order_id": oid, "tracking": s["tracking"]})
                else:
                    blocked.append({"order_id": oid, "reason": s["error"]})
    return {"shipped": shipped, "blocked": blocked}
''',
}
