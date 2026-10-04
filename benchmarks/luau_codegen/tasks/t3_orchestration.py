"""T3 — multi-tool orchestration: loops, conditional side effects, call budgets.

This is the LunarDyson thesis: one generated program replaces several
LLM → tool → LLM round-trips.
"""

from benchmarks.luau_codegen.tasks.t2_single_tool import ERR_DOC, TOOL_ERROR

_OK_DOC = "Returns {ok = true} on success."

TASKS: list[dict] = [
    {
        "id": "t3_notify_inactive",
        "goal": (
            "List customers of a segment and send the message 'Hi <name>, we miss you!' to every "
            "customer with last_seen_days >= inactive_days (user = customer id). Return notified "
            "(ids whose send succeeded, in list order), failed (ids whose send failed) and count "
            "(= number notified). If listing fails, return {error, message} from the tool. "
            "tools.message.send may be called at most 10 times per execution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"segment": {"type": "string"}, "inactive_days": {"type": "integer"}},
            "required": ["segment", "inactive_days"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "notified": {"type": "array", "items": {"type": "string"}},
                "failed": {"type": "array", "items": {"type": "string"}},
                "count": {"type": "integer"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type CustomerRef = {{ id: string, name: string, last_seen_days: number }}
type Input = {{ segment: string, inactive_days: number }}
type Output = {{ notified: {{ string }}, failed: {{ string }}, count: number }}
    | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "crm.list",
                "signature": "(args: { segment: string }) -> { ok: true, customers: { CustomerRef } } | ToolError",
                "description": (
                    "args {segment}. Returns {ok = true, customers = array of {id, name, "
                    "last_seen_days}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.segment == "dormant" then
    return { ok = true, customers = {
      { id = "c1", name = "Ana", last_seen_days = 45 },
      { id = "c2", name = "Bruno", last_seen_days = 3 },
      { id = "c3", name = "Caio", last_seen_days = 30 },
      { id = "c4", name = "Duda", last_seen_days = 90 },
      { id = "c5", name = "Eva", last_seen_days = 29 },
    } }
  end
  return { ok = false, error = "unknown_segment", message = "segment does not exist" }
end
""",
            },
            {
                "name": "message.send",
                "signature": "(args: { user: string, text: string }) -> { ok: true, message_id: string } | ToolError",
                "description": "args {user, text}. Returns {ok = true, message_id}. " + ERR_DOC,
                "max_calls": 10,
                "stub": """
function(args)
  if type(args.text) ~= "string" or not string.find(args.text, "^Hi %a+, we miss you!$") then
    return { ok = false, error = "bad_text", message = "unexpected text: " .. tostring(args.text) }
  end
  if args.user == "c4" then
    return { ok = false, error = "blocked", message = "user blocked messages" }
  end
  return { ok = true, message_id = "m-" .. tostring(args.user) }
end
""",
            },
        ],
        "test_cases": [
            {
                "input": {"segment": "dormant", "inactive_days": 30},
                "expected": {"notified": ["c1", "c3"], "failed": ["c4"], "count": 2},
                "expected_calls": {"message.send": 3, "crm.list": 1},
            },
            {
                "input": {"segment": "dormant", "inactive_days": 100},
                "expected": {"notified": [], "failed": [], "count": 0},
                "expected_calls": {"message.send": 0},
            },
            {
                "input": {"segment": "ghost", "inactive_days": 1},
                "expected": {"error": "unknown_segment"},
                "expected_calls": {"message.send": 0},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.crm.list({ segment = input.segment })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local notified: { string }, failed: { string } = {}, {}
    for _, c in r.customers do
        if c.last_seen_days >= input.inactive_days then
            local sent = tools.message.send({ user = c.id, text = `Hi {c.name}, we miss you!` })
            table.insert(if sent.ok then notified else failed, c.id)
        end
    end
    return { notified = notified, failed = failed, count = #notified }
end
""",
    },
    {
        "id": "t3_price_drop_watch",
        "goal": (
            "For each symbol, fetch its price in `currency` and compute change_pct = (price - "
            "reference[symbol]) / reference[symbol] * 100 rounded to 2 decimals. Symbols with "
            "change_pct <= -threshold_pct go to dropped as {symbol, change_pct} (input order). "
            "Symbols whose price lookup fails go to errors (input order). If dropped is non-empty, "
            "call notify.send exactly once with a text summarizing the drops. Return {dropped, "
            "errors, notified = whether notify.send succeeded}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "currency": {"type": "string"},
                "reference": {"type": "object", "additionalProperties": {"type": "number"}},
                "threshold_pct": {"type": "number"},
            },
            "required": ["symbols", "currency", "reference", "threshold_pct"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "dropped": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "change_pct": {"type": "number"},
                        },
                    },
                },
                "errors": {"type": "array", "items": {"type": "string"}},
                "notified": {"type": "boolean"},
            },
            "required": ["dropped", "errors", "notified"],
        },
        "types": f"""
{TOOL_ERROR}
type PriceResult = {{ ok: true, symbol: string, currency: string, price: number }} | ToolError
type Drop = {{ symbol: string, change_pct: number }}
type Input = {{
    symbols: {{ string }}, currency: string, reference: {{ [string]: number }}, threshold_pct: number,
}}
type Output = {{ dropped: {{ Drop }}, errors: {{ string }}, notified: boolean }}
""",
        "tools": [
            {
                "name": "market.get_price",
                "signature": "(args: { symbol: string, currency: string }) -> PriceResult",
                "description": (
                    "args {symbol, currency}. Returns {ok = true, symbol, currency, price}. "
                    + ERR_DOC
                ),
                "stub": """
function(args)
  local prices = { BTC = 95000, ETH = 2500, SOL = 180 }
  local p = prices[args.symbol]
  if p == nil or args.currency ~= "USD" then
    return { ok = false, error = "not_found", message = "unknown symbol" }
  end
  return { ok = true, symbol = args.symbol, currency = args.currency, price = p }
end
""",
            },
            {
                "name": "notify.send",
                "signature": "(args: { text: string }) -> { ok: true } | ToolError",
                "description": "args {text}. " + _OK_DOC + " " + ERR_DOC,
                "max_calls": 1,
                "stub": """
function(args)
  if type(args.text) ~= "string" or args.text == "" then
    return { ok = false, error = "bad_text", message = "text required" }
  end
  return { ok = true }
end
""",
            },
        ],
        "test_cases": [
            {
                "input": {
                    "symbols": ["BTC", "ETH", "SOL", "ADA"],
                    "currency": "USD",
                    "reference": {"BTC": 100000, "ETH": 2550, "SOL": 170, "ADA": 1},
                    "threshold_pct": 3,
                },
                "expected": {
                    "dropped": [{"symbol": "BTC", "change_pct": -5}],
                    "errors": ["ADA"],
                    "notified": True,
                },
                "expected_calls": {"market.get_price": 4, "notify.send": 1},
            },
            {
                "input": {
                    "symbols": ["BTC", "ETH", "SOL", "ADA"],
                    "currency": "USD",
                    "reference": {"BTC": 100000, "ETH": 2550, "SOL": 170, "ADA": 1},
                    "threshold_pct": 1.5,
                },
                "expected": {
                    "dropped": [
                        {"symbol": "BTC", "change_pct": -5},
                        {"symbol": "ETH", "change_pct": -1.96},
                    ],
                    "errors": ["ADA"],
                    "notified": True,
                },
                "expected_calls": {"notify.send": 1},
            },
            {
                "input": {
                    "symbols": ["BTC", "SOL"],
                    "currency": "USD",
                    "reference": {"BTC": 100000, "SOL": 170},
                    "threshold_pct": 10,
                },
                "expected": {"dropped": [], "errors": [], "notified": False},
                "expected_calls": {"notify.send": 0},
            },
        ],
        "reference": """
local function round2(x: number): number
    return math.floor(x * 100 + 0.5) / 100
end

function run(input: Input, context: Context): Output
    local dropped: { Drop }, errors: { string } = {}, {}
    for _, symbol in input.symbols do
        local r = tools.market.get_price({ symbol = symbol, currency = input.currency })
        if not r.ok then
            table.insert(errors, symbol)
        else
            local ref = input.reference[symbol]
            local change = round2((r.price - ref) / ref * 100)
            if change <= -input.threshold_pct then
                table.insert(dropped, { symbol = symbol, change_pct = change })
            end
        end
    end
    local notified = false
    if #dropped > 0 then
        local lines: { string } = {}
        for _, d in dropped do
            table.insert(lines, `{d.symbol} {d.change_pct}%`)
        end
        notified = tools.notify.send({ text = "Price drops: " .. table.concat(lines, ", ") }).ok
    end
    return { dropped = dropped, errors = errors, notified = notified }
end
""",
    },
    {
        "id": "t3_budgeted_sync",
        "goal": (
            "Fetch customers by id, in order. tools.crm.get may be called at most 5 times per "
            "execution (exceeding the budget aborts the run), so only the first 5 ids are fetched "
            "and the remaining ids go to skipped. Return processed (ids fetched successfully), "
            "not_found (ids whose fetch failed), skipped, and total_balance (sum of the balances "
            "of processed customers)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "processed": {"type": "array", "items": {"type": "string"}},
                "not_found": {"type": "array", "items": {"type": "string"}},
                "skipped": {"type": "array", "items": {"type": "string"}},
                "total_balance": {"type": "number"},
            },
            "required": ["processed", "not_found", "skipped", "total_balance"],
        },
        "types": f"""
{TOOL_ERROR}
type Customer = {{ id: string, name: string, balance: number }}
type Input = {{ ids: {{ string }} }}
type Output = {{
    processed: {{ string }}, not_found: {{ string }}, skipped: {{ string }}, total_balance: number,
}}
""",
        "tools": [
            {
                "name": "crm.get",
                "signature": "(args: { id: string }) -> { ok: true, customer: Customer } | ToolError",
                "description": (
                    "args {id}. Returns {ok = true, customer = {id, name, balance}}. " + ERR_DOC
                ),
                "max_calls": 5,
                "stub": """
function(args)
  local balances = { c1 = 10.5, c2 = 20, c3 = 0, c4 = 5.25, c5 = 100, c6 = 7 }
  local b = balances[args.id]
  if b == nil then
    return { ok = false, error = "not_found", message = "no such customer" }
  end
  return { ok = true, customer = { id = args.id, name = "Customer " .. args.id, balance = b } }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"ids": ["c1", "c2", "cX", "c3", "c4", "c5", "c6"]},
                "expected": {
                    "processed": ["c1", "c2", "c3", "c4"],
                    "not_found": ["cX"],
                    "skipped": ["c5", "c6"],
                    "total_balance": 35.75,
                },
                "expected_calls": {"crm.get": 5},
            },
            {
                "input": {"ids": ["c5"]},
                "expected": {
                    "processed": ["c5"],
                    "not_found": [],
                    "skipped": [],
                    "total_balance": 100,
                },
            },
        ],
        "reference": """
local BUDGET = 5

function run(input: Input, context: Context): Output
    local processed: { string }, notFound: { string }, skipped: { string } = {}, {}, {}
    local total = 0
    for i, id in input.ids do
        if i > BUDGET then
            table.insert(skipped, id)
            continue
        end
        local r = tools.crm.get({ id = id })
        if r.ok then
            table.insert(processed, id)
            total += r.customer.balance
        else
            table.insert(notFound, id)
        end
    end
    return { processed = processed, not_found = notFound, skipped = skipped, total_balance = total }
end
""",
    },
    {
        "id": "t3_ticket_triage",
        "goal": (
            "List tickets with the given status and compute each ticket's priority: 'p1' if the "
            "title or body contains 'outage' or 'down' (case-insensitive substring), else 'p2' if "
            "it contains 'error' or 'bug', else 'p3'. Call tickets.update only for tickets whose "
            "computed priority differs from the current one. Return updated (list of {id, "
            "priority}, in list order) and unchanged (count of tickets not updated). If listing "
            "fails, return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "updated": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "priority": {"type": "string"}},
                    },
                },
                "unchanged": {"type": "integer"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Ticket = {{ id: string, title: string, body: string, priority: string }}
type Update = {{ id: string, priority: string }}
type Input = {{ status: string }}
type Output = {{ updated: {{ Update }}, unchanged: number }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "tickets.list",
                "signature": "(args: { status: string }) -> { ok: true, tickets: { Ticket } } | ToolError",
                "description": (
                    "args {status}. Returns {ok = true, tickets = array of {id, title, body, "
                    "priority}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.status == "open" then
    return { ok = true, tickets = {
      { id = "T1", title = "Site outage in EU", body = "all pages fail", priority = "p3" },
      { id = "T2", title = "Typo on pricing page", body = "minor", priority = "p3" },
      { id = "T3", title = "Checkout Error 500", body = "users see an error", priority = "p1" },
      { id = "T4", title = "API is DOWN", body = "", priority = "p1" },
      { id = "T5", title = "Feature request", body = "Also found a BUG in export", priority = "p3" },
    } }
  elseif args.status == "closed" then
    return { ok = true, tickets = {} }
  end
  return { ok = false, error = "bad_status", message = "unknown status" }
end
""",
            },
            {
                "name": "tickets.update",
                "signature": "(args: { id: string, priority: string }) -> { ok: true } | ToolError",
                "description": "args {id, priority}. " + _OK_DOC + " " + ERR_DOC,
                "stub": """
function(args)
  if args.priority ~= "p1" and args.priority ~= "p2" and args.priority ~= "p3" then
    return { ok = false, error = "bad_priority", message = "invalid priority" }
  end
  return { ok = true }
end
""",
            },
        ],
        "test_cases": [
            {
                "input": {"status": "open"},
                "expected": {
                    "updated": [
                        {"id": "T1", "priority": "p1"},
                        {"id": "T3", "priority": "p2"},
                        {"id": "T5", "priority": "p2"},
                    ],
                    "unchanged": 2,
                },
                "expected_calls": {"tickets.update": 3},
            },
            {
                "input": {"status": "closed"},
                "expected": {"updated": [], "unchanged": 0},
                "expected_calls": {"tickets.update": 0},
            },
            {"input": {"status": "weird"}, "expected": {"error": "bad_status"}},
        ],
        "reference": """
local function contains(text: string, words: { string }): boolean
    for _, w in words do
        if string.find(text, w, 1, true) then
            return true
        end
    end
    return false
end

function run(input: Input, context: Context): Output
    local r = tools.tickets.list({ status = input.status })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local updated: { Update } = {}
    local unchanged = 0
    for _, t in r.tickets do
        local text = string.lower(t.title .. " " .. t.body)
        local priority = if contains(text, { "outage", "down" })
            then "p1"
            elseif contains(text, { "error", "bug" }) then "p2"
            else "p3"
        if priority ~= t.priority then
            tools.tickets.update({ id = t.id, priority = priority })
            table.insert(updated, { id = t.id, priority = priority })
        else
            unchanged += 1
        end
    end
    return { updated = updated, unchanged = unchanged }
end
""",
    },
    {
        "id": "t3_order_fulfillment",
        "goal": (
            "Process orders in input order. For each id: fetch the order (failure -> blocked with "
            "reason 'not_found'); if status is not 'paid' -> blocked 'not_paid' without checking "
            "inventory; otherwise check inventory for each item in order and block with "
            "'out_of_stock:<sku>' at the first item whose available < qty; if every item is "
            "available, create a shipment and add {order_id, tracking} to shipped. Never create "
            "shipments for blocked orders. Return {shipped, blocked = list of {order_id, reason}}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["order_ids"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "shipped": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"},
                            "tracking": {"type": "string"},
                        },
                    },
                },
                "blocked": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["shipped", "blocked"],
        },
        "types": f"""
{TOOL_ERROR}
type OrderItem = {{ sku: string, qty: number }}
type Order = {{ id: string, status: string, items: {{ OrderItem }} }}
type Shipped = {{ order_id: string, tracking: string }}
type Blocked = {{ order_id: string, reason: string }}
type Input = {{ order_ids: {{ string }} }}
type Output = {{ shipped: {{ Shipped }}, blocked: {{ Blocked }} }}
""",
        "tools": [
            {
                "name": "orders.get",
                "signature": "(args: { id: string }) -> { ok: true, order: Order } | ToolError",
                "description": (
                    "args {id}. Returns {ok = true, order = {id, status, items = array of {sku, "
                    "qty}}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  local orders = {
    o1 = { id = "o1", status = "paid", items = { { sku = "A", qty = 2 }, { sku = "B", qty = 1 } } },
    o2 = { id = "o2", status = "pending", items = { { sku = "A", qty = 1 } } },
    o3 = { id = "o3", status = "paid", items = { { sku = "A", qty = 1 }, { sku = "C", qty = 5 } } },
    o4 = { id = "o4", status = "paid", items = { { sku = "B", qty = 3 } } },
  }
  local o = orders[args.id]
  if o == nil then
    return { ok = false, error = "not_found", message = "no such order" }
  end
  return { ok = true, order = o }
end
""",
            },
            {
                "name": "inventory.check",
                "signature": "(args: { sku: string }) -> { ok: true, available: number } | ToolError",
                "description": "args {sku}. Returns {ok = true, available}. " + ERR_DOC,
                "stub": """
function(args)
  local stock = { A = 10, B = 3, C = 2 }
  local s = stock[args.sku]
  if s == nil then
    return { ok = false, error = "unknown_sku", message = "sku not tracked" }
  end
  return { ok = true, available = s }
end
""",
            },
            {
                "name": "shipping.create",
                "signature": "(args: { order_id: string }) -> { ok: true, tracking: string } | ToolError",
                "description": "args {order_id}. Returns {ok = true, tracking}. " + ERR_DOC,
                "stub": """
function(args)
  if args.order_id == "o2" or args.order_id == "o3" then
    return { ok = false, error = "not_allowed", message = "order must not ship" }
  end
  return { ok = true, tracking = "TRK-" .. tostring(args.order_id) }
end
""",
            },
        ],
        "test_cases": [
            {
                "input": {"order_ids": ["o1", "o2", "o3", "o9", "o4"]},
                "expected": {
                    "shipped": [
                        {"order_id": "o1", "tracking": "TRK-o1"},
                        {"order_id": "o4", "tracking": "TRK-o4"},
                    ],
                    "blocked": [
                        {"order_id": "o2", "reason": "not_paid"},
                        {"order_id": "o3", "reason": "out_of_stock:C"},
                        {"order_id": "o9", "reason": "not_found"},
                    ],
                },
                "expected_calls": {"shipping.create": 2, "orders.get": 5},
            },
            {"input": {"order_ids": []}, "expected": {"shipped": [], "blocked": []}},
        ],
        "reference": """
local function firstShortage(order: Order): string?
    for _, item in order.items do
        local stock = tools.inventory.check({ sku = item.sku })
        if not stock.ok or stock.available < item.qty then
            return item.sku
        end
    end
    return nil
end

function run(input: Input, context: Context): Output
    local shipped: { Shipped }, blocked: { Blocked } = {}, {}
    for _, id in input.order_ids do
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
                if s.ok then
                    table.insert(shipped, { order_id = id, tracking = s.tracking })
                else
                    table.insert(blocked, { order_id = id, reason = s.error })
                end
            end
        end
    end
    return { shipped = shipped, blocked = blocked }
end
""",
    },
]
