"""Per-execution overhead on this machine: LunarDyson vs Monty, same scenarios as
lunardyson/bench/overhead.py (empty run, 1 tool call, 13 tool calls, CPU loop).

    python -m benchmarks.luau_codegen.monty_overhead [out.json]

LunarDyson: one sealed runtime reused (its intended usage). Monty: a warm pool,
one checkout per execution (its intended usage: state never leaks between runs).
Also reports the Monty pool's RSS, a cold pool start and how fast a runaway loop stops.
"""

import json
import os
import platform
import statistics
import sys
import time

import lunardyson as ld
import pydantic_monty as pm

ORDERS = {
    "o1": {"id": "o1", "status": "paid", "items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]},
    "o2": {"id": "o2", "status": "pending", "items": [{"sku": "A", "qty": 1}]},
    "o3": {"id": "o3", "status": "paid", "items": [{"sku": "A", "qty": 1}, {"sku": "C", "qty": 5}]},
    "o4": {"id": "o4", "status": "paid", "items": [{"sku": "B", "qty": 3}]},
}
STOCK = {"A": 10, "B": 3, "C": 2}


def orders_get(args):
    order = ORDERS.get(args["id"])
    return (
        {"ok": True, "order": order}
        if order
        else {"ok": False, "error": "not_found", "message": "no order"}
    )


TOOLS = {
    "market.get_price": lambda args: {"ok": True, "symbol": args["symbol"], "price": 534000.12},
    "orders.get": orders_get,
    "inventory.check": lambda args: {"ok": True, "available": STOCK.get(args["sku"], 0)},
    "shipping.create": lambda args: {"ok": True, "tracking": "TRK-" + args["order_id"]},
}

LUAU_T3 = """
local function firstShortage(order)
    for _, item in ipairs(order.items) do
        local stock = tools.inventory.check({ sku = item.sku })
        if not stock.ok or stock.available < item.qty then return item.sku end
    end
    return nil
end
function run(input, context)
    local shipped, blocked = {}, {}
    for _, id in ipairs(input.order_ids) do
        local r = tools.orders.get({ id = id })
        if not r.ok then
            table.insert(blocked, { order_id = id, reason = "not_found" })
        elseif r.order.status ~= "paid" then
            table.insert(blocked, { order_id = id, reason = "not_paid" })
        else
            local sku = firstShortage(r.order)
            if sku then
                table.insert(blocked, { order_id = id, reason = "out_of_stock:" .. sku })
            else
                local s = tools.shipping.create({ order_id = id })
                table.insert(shipped, { order_id = id, tracking = s.tracking })
            end
        end
    end
    return { shipped = shipped, blocked = blocked }
end
"""

PY_T3 = """
def first_shortage(order):
    for item in order["items"]:
        stock = tools.inventory.check({"sku": item["sku"]})
        if not stock["ok"] or stock["available"] < item["qty"]:
            return item["sku"]
    return None

def run(input, context):
    shipped, blocked = [], []
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
                shipped.append({"order_id": oid, "tracking": s["tracking"]})
    return {"shipped": shipped, "blocked": blocked}
"""

SCENARIOS = [
    (
        "empty run",
        "function run(input, context) return { ok = true } end",
        "def run(input, context):\n    return {'ok': True}\n",
        {},
        1000,
    ),
    (
        "1 tool call",
        (
            "function run(input, context) local p = tools.market.get_price({ symbol = input.symbol }) "
            "return { price = p.price } end"
        ),
        (
            "def run(input, context):\n    p = tools.market.get_price({'symbol': input['symbol']})\n"
            "    return {'price': p['price']}\n"
        ),
        {"symbol": "BTC"},
        1000,
    ),
    (
        "T3 order fulfillment (13 tool calls)",
        LUAU_T3,
        PY_T3,
        {"order_ids": ["o1", "o2", "o3", "o9", "o4"]},
        500,
    ),
    (
        "CPU: 200k loop iterations",
        "function run(input) local s = 0 for i = 1, 200000 do s = s + i % 7 end return { s = s } end",
        (
            "def run(input, context):\n    s = 0\n    for i in range(1, 200001):\n        s = s + i % 7\n"
            "    return {'s': s}\n"
        ),
        {},
        50,
    ),
]
LIMITS: pm.ResourceLimits = {"max_feed_duration_secs": 5.0, "max_memory": 16 * 1024 * 1024}


def _modules() -> dict:
    grouped: dict[str, dict] = {}
    for name, fn in TOOLS.items():
        ns, f = name.split(".")
        grouped.setdefault(ns, {})[f] = fn
    return {"tools": pm.ExternalModule({ns: pm.ExternalModule(fns) for ns, fns in grouped.items()})}


def rss_mb(pids) -> float:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
        except FileNotFoundError:
            pass
    return round(total / 1024, 1)


def children(pid: int) -> list[int]:
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/stat") as f:
                    if int(f.read().rsplit(")", 1)[1].split()[1]) == pid:
                        out.append(int(p))
            except (FileNotFoundError, IndexError, ValueError):
                pass
    return out


def main() -> int:
    rows = []
    rt = ld.Runtime(cpu_time_ms=5000)
    for name, fn in TOOLS.items():
        rt.tool(name, fn)

    t = time.perf_counter()
    pool = pm.Monty(min_processes=1, max_processes=1).__enter__()
    with pool.checkout(limits=LIMITS, external_modules=_modules()) as s:
        s.feed_run("1")
    cold_ms = (time.perf_counter() - t) * 1000

    def ld_once(src, inp):
        r = rt.execute(src, inp)
        assert r.ok, r.message

    def monty_once(src, inp):
        with pool.checkout(limits=LIMITS, external_modules=_modules()) as s:
            s.feed_run("import tools\n" + src)
            s.feed_run("run(__i, {'now': 'x'})", inputs={"__i": inp})

    def monty_single(src, inp):  # one feed: define and call in the same round trip
        with pool.checkout(limits=LIMITS, external_modules=_modules()) as s:
            s.feed_run("import tools\n" + src + "\nrun(__i, {'now': 'x'})", inputs={"__i": inp})

    for label, luau, py, inp, n in SCENARIOS:
        fns = {
            "lunardyson": lambda luau=luau, inp=inp: ld_once(luau, inp),
            "monty": lambda py=py, inp=inp: monty_once(py, inp),
            "monty 1-feed": lambda py=py, inp=inp: monty_single(py, inp),
        }
        for fn in fns.values():
            for _ in range(min(20, n)):
                fn()
        samples = {k: [] for k in fns}
        for _ in range(n):
            for k, fn in fns.items():
                t = time.perf_counter()
                fn()
                samples[k].append(time.perf_counter() - t)
        for k, xs in samples.items():
            rows.append(
                {"scenario": label, "engine": k, "median_us": round(statistics.median(xs) * 1e6, 1)}
            )
            print(f"{label:40s} {k:12s} {statistics.median(xs) * 1e6:10.1f} µs")

    workers = children(os.getpid())
    rss = {
        "host_python_mb": rss_mb([os.getpid()]),
        "monty_workers_mb": rss_mb(workers),
        "workers": len(workers),
    }

    loop = {}
    with pool.checkout(limits={"max_feed_duration_secs": 0.2}) as s:
        t = time.perf_counter()
        try:
            s.feed_run("try:\n    while True:\n        pass\nexcept Exception:\n    pass\n")
        except pm.MontyError as e:
            loop["monty"] = {
                "stopped_ms": round((time.perf_counter() - t) * 1000, 1),
                "error": type(e).__name__,
            }
    rt2 = ld.Runtime(cpu_time_ms=200)
    t = time.perf_counter()
    r = rt2.execute("function run() while true do pcall(function() end) end end", {})
    loop["lunardyson"] = {
        "stopped_ms": round((time.perf_counter() - t) * 1000, 1),
        "error": r.error_kind,
    }
    pool.__exit__(None, None, None)

    meta = {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "monty": __import__("importlib.metadata").metadata.version("pydantic-monty"),
        "monty_cold_pool_ms": round(cold_ms, 1),
        "rss": rss,
        "runaway_loop_200ms": loop,
    }
    print(json.dumps(meta, indent=2))
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            json.dump({"meta": meta, "rows": rows}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
