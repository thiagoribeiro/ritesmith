"""T2 — one tool (possibly called several times) plus filtering/formatting/error handling.

Tool stubs are written in the common subset of Lua 5.5 and Luau: they run
unchanged under both targets.
"""

TOOL_ERROR = "type ToolError = { ok: false, error: string, message: string }"
ERR_DOC = "On failure returns {ok = false, error = <code>, message = <text>}."

TASKS: list[dict] = [
    {
        "id": "t2_price_label",
        "goal": (
            "Fetch the price of `symbol` in `currency` and return {symbol, currency, price "
            "rounded to 2 decimals, label = '<SYMBOL>: <price with exactly 2 decimals> "
            "<CURRENCY>'}. If the tool fails, return {error, message} taken from the tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "currency": {"type": "string"}},
            "required": ["symbol", "currency"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "currency": {"type": "string"},
                "price": {"type": "number"},
                "label": {"type": "string"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type PriceResult = {{ ok: true, symbol: string, currency: string, price: number }} | ToolError
type Input = {{ symbol: string, currency: string }}
type Output = {{ symbol: string, currency: string, price: number, label: string }}
    | {{ error: string, message: string }}
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
  local prices = { BTC_BRL = 534000.123, ETH_USD = 2500.5, BTC_USD = 98765.4321 }
  local p = prices[tostring(args.symbol) .. "_" .. tostring(args.currency)]
  if p == nil then
    return { ok = false, error = "not_found", message = "unknown pair" }
  end
  return { ok = true, symbol = args.symbol, currency = args.currency, price = p }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"symbol": "BTC", "currency": "BRL"},
                "expected": {
                    "symbol": "BTC",
                    "currency": "BRL",
                    "price": 534000.12,
                    "label": "BTC: 534000.12 BRL",
                },
                "expected_calls": {"market.get_price": 1},
            },
            {
                "input": {"symbol": "ETH", "currency": "USD"},
                "expected": {"price": 2500.5, "label": "ETH: 2500.50 USD"},
            },
            {
                "input": {"symbol": "DOGE", "currency": "BRL"},
                "expected": {"error": "not_found", "label": None},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.market.get_price({ symbol = input.symbol, currency = input.currency })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    return {
        symbol = r.symbol,
        currency = r.currency,
        price = math.floor(r.price * 100 + 0.5) / 100,
        label = string.format("%s: %.2f %s", r.symbol, r.price, r.currency),
    }
end
""",
    },
    {
        "id": "t2_customer_summary",
        "goal": (
            "Load a customer and return {id, display = '<name> <<email>>', status = 'active' or "
            "'inactive', vip = true if the customer's tags contain 'vip'}. If the tool fails, "
            "return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "display": {"type": "string"},
                "status": {"enum": ["active", "inactive"]},
                "vip": {"type": "boolean"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Customer = {{
    id: string, name: string, email: string, active: boolean, balance: number, tags: {{ string }},
}}
type Input = {{ id: string }}
type Output = {{ id: string, display: string, status: string, vip: boolean }}
    | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "crm.get",
                "signature": "(args: { id: string }) -> { ok: true, customer: Customer } | ToolError",
                "description": (
                    "args {id}. Returns {ok = true, customer = {id, name, email, active: boolean, "
                    "balance: number, tags: array of strings}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  local db = {
    c1 = { id = "c1", name = "Ana Souza", email = "ana@x.com", active = true, balance = 10.5,
           tags = { "early", "vip" } },
    c2 = { id = "c2", name = "Bruno Lima", email = "bruno@y.com", active = false, balance = 0,
           tags = {} },
  }
  local c = db[args.id]
  if c == nil then
    return { ok = false, error = "not_found", message = "no customer " .. tostring(args.id) }
  end
  return { ok = true, customer = c }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"id": "c1"},
                "expected": {
                    "id": "c1",
                    "display": "Ana Souza <ana@x.com>",
                    "status": "active",
                    "vip": True,
                },
            },
            {
                "input": {"id": "c2"},
                "expected": {
                    "id": "c2",
                    "display": "Bruno Lima <bruno@y.com>",
                    "status": "inactive",
                    "vip": False,
                },
            },
            {"input": {"id": "c9"}, "expected": {"error": "not_found"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.crm.get({ id = input.id })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local c = r.customer
    return {
        id = c.id,
        display = `{c.name} <{c.email}>`,
        status = if c.active then "active" else "inactive",
        vip = table.find(c.tags, "vip") ~= nil,
    }
end
""",
    },
    {
        "id": "t2_weather_alert",
        "goal": (
            "Get current weather at (lat, lon) and decide whether to alert. reasons, in this "
            "order: 'high_wind' if wind_kmh > max_wind_kmh, 'freezing' if temp_c <= 0, 'storm' "
            "if condition == 'storm'. alert is true when reasons is non-empty. If the tool fails, "
            "return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "max_wind_kmh": {"type": "number"},
            },
            "required": ["lat", "lon", "max_wind_kmh"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "alert": {"type": "boolean"},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Weather = {{ ok: true, temp_c: number, wind_kmh: number, condition: string }}
type Input = {{ lat: number, lon: number, max_wind_kmh: number }}
type Output = {{ alert: boolean, reasons: {{ string }} }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "weather.current",
                "signature": "(args: { lat: number, lon: number }) -> Weather | ToolError",
                "description": (
                    "args {lat, lon}. Returns {ok = true, temp_c, wind_kmh, condition} where "
                    "condition is e.g. 'clear', 'rain', 'storm'. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.lat == -23.5 then
    return { ok = true, temp_c = 24, wind_kmh = 12, condition = "clear" }
  elseif args.lat == 60 then
    return { ok = true, temp_c = -3, wind_kmh = 55, condition = "storm" }
  elseif args.lat == 10 then
    return { ok = true, temp_c = 0, wind_kmh = 20, condition = "rain" }
  end
  return { ok = false, error = "out_of_range", message = "no station near coordinates" }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"lat": -23.5, "lon": -46.6, "max_wind_kmh": 40},
                "expected": {"alert": False, "reasons": []},
            },
            {
                "input": {"lat": 60, "lon": 10, "max_wind_kmh": 50},
                "expected": {"alert": True, "reasons": ["high_wind", "freezing", "storm"]},
            },
            {
                "input": {"lat": 10, "lon": 10, "max_wind_kmh": 20},
                "expected": {"alert": True, "reasons": ["freezing"]},
            },
            {
                "input": {"lat": 89, "lon": 0, "max_wind_kmh": 10},
                "expected": {"error": "out_of_range"},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local w = tools.weather.current({ lat = input.lat, lon = input.lon })
    if not w.ok then
        return { error = w.error, message = w.message }
    end
    local reasons: { string } = {}
    if w.wind_kmh > input.max_wind_kmh then
        table.insert(reasons, "high_wind")
    end
    if w.temp_c <= 0 then
        table.insert(reasons, "freezing")
    end
    if w.condition == "storm" then
        table.insert(reasons, "storm")
    end
    return { alert = #reasons > 0, reasons = reasons }
end
""",
    },
    {
        "id": "t2_open_bugs",
        "goal": (
            "List the issues of a repository and return open_count (issues with state 'open') "
            "and bug_numbers: numbers of open issues labeled 'bug', sorted ascending. If the "
            "tool fails, return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "open_count": {"type": "integer"},
                "bug_numbers": {"type": "array", "items": {"type": "integer"}},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Issue = {{ number: number, title: string, state: string, labels: {{ string }} }}
type Input = {{ repo: string }}
type Output = {{ open_count: number, bug_numbers: {{ number }} }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "github.list_issues",
                "signature": "(args: { repo: string }) -> { ok: true, issues: { Issue } } | ToolError",
                "description": (
                    "args {repo}. Returns {ok = true, issues = array of {number, title, state "
                    "('open' or 'closed'), labels: array of strings}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.repo == "acme/api" then
    return { ok = true, issues = {
      { number = 12, title = "Crash on login", state = "open", labels = { "bug" } },
      { number = 3, title = "Timeout", state = "open", labels = { "p1", "bug" } },
      { number = 7, title = "Old bug", state = "closed", labels = { "bug" } },
      { number = 9, title = "Dark mode", state = "open", labels = { "feature" } },
      { number = 15, title = "Docs", state = "open", labels = {} },
    } }
  elseif args.repo == "acme/empty" then
    return { ok = true, issues = {} }
  end
  return { ok = false, error = "not_found", message = "repository not found" }
end
""",
            }
        ],
        "test_cases": [
            {"input": {"repo": "acme/api"}, "expected": {"open_count": 4, "bug_numbers": [3, 12]}},
            {"input": {"repo": "acme/empty"}, "expected": {"open_count": 0, "bug_numbers": []}},
            {"input": {"repo": "acme/nope"}, "expected": {"error": "not_found"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.github.list_issues({ repo = input.repo })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local open, bugs = 0, {} :: { number }
    for _, issue in r.issues do
        if issue.state == "open" then
            open += 1
            if table.find(issue.labels, "bug") then
                table.insert(bugs, issue.number)
            end
        end
    end
    table.sort(bugs)
    return { open_count = open, bug_numbers = bugs }
end
""",
    },
    {
        "id": "t2_fx_convert",
        "goal": (
            "Convert amount from currency `from` to `to`. Return {amount, rate, converted = "
            "amount * rate rounded to 2 decimals}. When from == to, do NOT call the tool: rate is "
            "1. If the tool fails, return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["amount", "from", "to"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "rate": {"type": "number"},
                "converted": {"type": "number"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Input = {{ amount: number, from: string, to: string }}
type Output = {{ amount: number, rate: number, converted: number }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "fx.rate",
                "signature": "(args: { from: string, to: string }) -> { ok: true, rate: number } | ToolError",
                "description": "args {from, to}. Returns {ok = true, rate}. " + ERR_DOC,
                "stub": """
function(args)
  local rates = { USD_BRL = 5.4321, EUR_USD = 1.085 }
  local r = rates[tostring(args.from) .. "_" .. tostring(args.to)]
  if r == nil then
    return { ok = false, error = "unsupported_pair", message = "no rate available" }
  end
  return { ok = true, rate = r }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"amount": 100, "from": "USD", "to": "BRL"},
                "expected": {"amount": 100, "rate": 5.4321, "converted": 543.21},
                "expected_calls": {"fx.rate": 1},
            },
            {
                "input": {"amount": 10, "from": "EUR", "to": "USD"},
                "expected": {"rate": 1.085, "converted": 10.85},
            },
            {
                "input": {"amount": 42, "from": "BRL", "to": "BRL"},
                "expected": {"amount": 42, "rate": 1, "converted": 42},
                "expected_calls": {"fx.rate": 0},
            },
            {
                "input": {"amount": 5, "from": "USD", "to": "JPY"},
                "expected": {"error": "unsupported_pair"},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local rate = 1
    if input.from ~= input.to then
        local r = tools.fx.rate({ from = input.from, to = input.to })
        if not r.ok then
            return { error = r.error, message = r.message }
        end
        rate = r.rate
    end
    local converted = math.floor(input.amount * rate * 100 + 0.5) / 100
    return { amount = input.amount, rate = rate, converted = converted }
end
""",
    },
    {
        "id": "t2_low_stock",
        "goal": (
            "List warehouse items and return low: items whose qty < min_qty as {sku, missing = "
            "min_qty - qty}, sorted by missing descending, then sku ascending. If the tool fails, "
            "return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"warehouse": {"type": "string"}},
            "required": ["warehouse"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "low": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}, "missing": {"type": "integer"}},
                    },
                },
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type StockItem = {{ sku: string, qty: number, min_qty: number }}
type Low = {{ sku: string, missing: number }}
type Input = {{ warehouse: string }}
type Output = {{ low: {{ Low }} }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "inventory.list",
                "signature": "(args: { warehouse: string }) -> { ok: true, items: { StockItem } } | ToolError",
                "description": (
                    "args {warehouse}. Returns {ok = true, items = array of {sku, qty, min_qty}}. "
                    + ERR_DOC
                ),
                "stub": """
function(args)
  if args.warehouse == "sp-01" then
    return { ok = true, items = {
      { sku = "D4", qty = 2, min_qty = 7 },
      { sku = "A1", qty = 5, min_qty = 10 },
      { sku = "B2", qty = 0, min_qty = 3 },
      { sku = "C3", qty = 20, min_qty = 5 },
      { sku = "E5", qty = 3, min_qty = 3 },
    } }
  elseif args.warehouse == "rj-01" then
    return { ok = true, items = {} }
  end
  return { ok = false, error = "not_found", message = "unknown warehouse" }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"warehouse": "sp-01"},
                "expected": {
                    "low": [
                        {"sku": "A1", "missing": 5},
                        {"sku": "D4", "missing": 5},
                        {"sku": "B2", "missing": 3},
                    ]
                },
            },
            {"input": {"warehouse": "rj-01"}, "expected": {"low": []}},
            {"input": {"warehouse": "xx"}, "expected": {"error": "not_found"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.inventory.list({ warehouse = input.warehouse })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local low: { Low } = {}
    for _, item in r.items do
        if item.qty < item.min_qty then
            table.insert(low, { sku = item.sku, missing = item.min_qty - item.qty })
        end
    end
    table.sort(low, function(a: Low, b: Low)
        if a.missing ~= b.missing then
            return a.missing > b.missing
        end
        return a.sku < b.sku
    end)
    return { low = low }
end
""",
    },
    {
        "id": "t2_free_slots",
        "goal": (
            "Find free time slots on a date. Events come unsorted and may overlap or extend past "
            "the working day. Return slots: gaps between day_start and day_end not covered by any "
            "event, lasting at least min_minutes, as {from = 'HH:MM', to = 'HH:MM'} in "
            "chronological order. If the tool fails, return {error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "day_start": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                "day_end": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                "min_minutes": {"type": "integer"},
            },
            "required": ["date", "day_start", "day_end", "min_minutes"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                    },
                },
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Event = {{ title: string, starts_at: string, ends_at: string }}
type Slot = {{ from: string, to: string }}
type Input = {{ date: string, day_start: string, day_end: string, min_minutes: number }}
type Output = {{ slots: {{ Slot }} }} | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "calendar.events",
                "signature": "(args: { date: string }) -> { ok: true, events: { Event } } | ToolError",
                "description": (
                    "args {date = 'YYYY-MM-DD'}. Returns {ok = true, events = array of {title, "
                    "starts_at = 'HH:MM', ends_at = 'HH:MM'}}, unsorted. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.date == "2026-09-30" then
    return { ok = true, events = {
      { title = "standup", starts_at = "09:00", ends_at = "10:30" },
      { title = "review", starts_at = "13:00", ends_at = "14:00" },
      { title = "1:1", starts_at = "10:15", ends_at = "11:00" },
      { title = "late call", starts_at = "17:30", ends_at = "18:30" },
    } }
  elseif args.date == "2026-10-01" then
    return { ok = true, events = {} }
  end
  return { ok = false, error = "calendar_unavailable", message = "try later" }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {
                    "date": "2026-09-30",
                    "day_start": "08:00",
                    "day_end": "18:00",
                    "min_minutes": 30,
                },
                "expected": {
                    "slots": [
                        {"from": "08:00", "to": "09:00"},
                        {"from": "11:00", "to": "13:00"},
                        {"from": "14:00", "to": "17:30"},
                    ]
                },
            },
            {
                "input": {
                    "date": "2026-09-30",
                    "day_start": "08:00",
                    "day_end": "18:00",
                    "min_minutes": 90,
                },
                "expected": {
                    "slots": [{"from": "11:00", "to": "13:00"}, {"from": "14:00", "to": "17:30"}]
                },
            },
            {
                "input": {
                    "date": "2026-10-01",
                    "day_start": "09:00",
                    "day_end": "17:00",
                    "min_minutes": 60,
                },
                "expected": {"slots": [{"from": "09:00", "to": "17:00"}]},
            },
            {
                "input": {
                    "date": "2026-12-25",
                    "day_start": "09:00",
                    "day_end": "17:00",
                    "min_minutes": 60,
                },
                "expected": {"error": "calendar_unavailable"},
            },
        ],
        "reference": """
local function toMinutes(hhmm: string): number
    local h, m = string.match(hhmm, "^(%d+):(%d+)$")
    return (tonumber(h) :: number) * 60 + (tonumber(m) :: number)
end

local function toClock(minutes: number): string
    return string.format("%02d:%02d", minutes // 60, minutes % 60)
end

function run(input: Input, context: Context): Output
    local r = tools.calendar.events({ date = input.date })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local busy: { { number } } = {}
    for _, e in r.events do
        table.insert(busy, { toMinutes(e.starts_at), toMinutes(e.ends_at) })
    end
    table.sort(busy, function(a: { number }, b: { number })
        return a[1] < b[1]
    end)
    local slots: { Slot } = {}
    local cursor, dayEnd = toMinutes(input.day_start), toMinutes(input.day_end)
    local function addSlot(from: number, to: number)
        if to - from >= input.min_minutes then
            table.insert(slots, { from = toClock(from), to = toClock(to) })
        end
    end
    for _, b in busy do
        if b[1] > cursor then
            addSlot(cursor, math.min(b[1], dayEnd))
        end
        cursor = math.max(cursor, b[2])
        if cursor >= dayEnd then
            break
        end
    end
    if cursor < dayEnd then
        addSlot(cursor, dayEnd)
    end
    return { slots = slots }
end
""",
    },
    {
        "id": "t2_url_health",
        "goal": (
            "Check each URL with http.get and split them into up (tool succeeded and 200 <= status "
            "< 300) and down (anything else, including tool errors), preserving input order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
            "required": ["urls"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "up": {"type": "array", "items": {"type": "string"}},
                "down": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["up", "down"],
        },
        "types": f"""
{TOOL_ERROR}
type Input = {{ urls: {{ string }} }}
type Output = {{ up: {{ string }}, down: {{ string }} }}
""",
        "tools": [
            {
                "name": "http.get",
                "signature": "(args: { url: string }) -> { ok: true, status: number, body: string } | ToolError",
                "description": (
                    "args {url}. Returns {ok = true, status, body} for any HTTP response. "
                    + ERR_DOC
                ),
                "stub": """
function(args)
  local statuses = {
    ["https://a.example/health"] = 200,
    ["https://b.example/health"] = 503,
    ["https://c.example/"] = 204,
    ["https://d.example/"] = 301,
  }
  local s = statuses[args.url]
  if s == nil then
    return { ok = false, error = "connection_refused", message = "cannot connect" }
  end
  return { ok = true, status = s, body = "" }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {
                    "urls": [
                        "https://a.example/health",
                        "https://b.example/health",
                        "https://c.example/",
                        "https://d.example/",
                        "https://e.example/",
                    ]
                },
                "expected": {
                    "up": ["https://a.example/health", "https://c.example/"],
                    "down": [
                        "https://b.example/health",
                        "https://d.example/",
                        "https://e.example/",
                    ],
                },
                "expected_calls": {"http.get": 5},
            },
            {"input": {"urls": []}, "expected": {"up": [], "down": []}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local up: { string }, down: { string } = {}, {}
    for _, url in input.urls do
        local r = tools.http.get({ url = url })
        if r.ok and r.status >= 200 and r.status < 300 then
            table.insert(up, url)
        else
            table.insert(down, url)
        end
    end
    return { up = up, down = down }
end
""",
    },
    {
        "id": "t2_config_resolve",
        "goal": (
            "Resolve each key with config.get. When the tool fails for a key, use defaults[key] "
            "if present; otherwise leave the key out of values and append it to missing "
            "(preserving key order)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}},
                "defaults": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["keys", "defaults"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "values": {"type": "object", "additionalProperties": {"type": "string"}},
                "missing": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["values", "missing"],
        },
        "types": f"""
{TOOL_ERROR}
type Input = {{ keys: {{ string }}, defaults: {{ [string]: string }} }}
type Output = {{ values: {{ [string]: string }}, missing: {{ string }} }}
""",
        "tools": [
            {
                "name": "config.get",
                "signature": "(args: { key: string }) -> { ok: true, value: string } | ToolError",
                "description": "args {key}. Returns {ok = true, value}. " + ERR_DOC,
                "stub": """
function(args)
  local store = { region = "sa-east-1", retries = "3" }
  local v = store[args.key]
  if v == nil then
    return { ok = false, error = "not_found", message = "key not set" }
  end
  return { ok = true, value = v }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {
                    "keys": ["region", "timeout", "retries", "owner"],
                    "defaults": {"timeout": "30s", "region": "us-east-1"},
                },
                "expected": {
                    "values": {
                        "region": "sa-east-1",
                        "timeout": "30s",
                        "retries": "3",
                        "owner": None,
                    },
                    "missing": ["owner"],
                },
                "expected_calls": {"config.get": 4},
            },
            {"input": {"keys": [], "defaults": {}}, "expected": {"values": {}, "missing": []}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local values: { [string]: string } = {}
    local missing: { string } = {}
    for _, key in input.keys do
        local r = tools.config.get({ key = key })
        if r.ok then
            values[key] = r.value
        elseif input.defaults[key] ~= nil then
            values[key] = input.defaults[key]
        else
            table.insert(missing, key)
        end
    end
    return { values = values, missing = missing }
end
""",
    },
    {
        "id": "t2_unread_digest",
        "goal": (
            "Summarize unread emails in a folder: count, senders (unique, sorted ascending) and "
            "latest_subject = subject of the message with the greatest received_at (ISO-8601 UTC "
            "strings; omit latest_subject when there are no messages). If the tool fails, return "
            "{error, message} from the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "required": ["folder"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "senders": {"type": "array", "items": {"type": "string"}},
                "latest_subject": {"type": "string"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type Email = {{ from: string, subject: string, received_at: string }}
type Input = {{ folder: string }}
type Output = {{ count: number, senders: {{ string }}, latest_subject: string? }}
    | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "email.list_unread",
                "signature": "(args: { folder: string }) -> { ok: true, messages: { Email } } | ToolError",
                "description": (
                    "args {folder}. Returns {ok = true, messages = array of {from, subject, "
                    "received_at}}. " + ERR_DOC
                ),
                "stub": """
function(args)
  if args.folder == "inbox" then
    return { ok = true, messages = {
      { from = "bob@y.com", subject = "Invoice", received_at = "2026-09-29T10:00:00Z" },
      { from = "ana@x.com", subject = "Deploy done", received_at = "2026-09-30T08:15:00Z" },
      { from = "bob@y.com", subject = "Re: Invoice", received_at = "2026-09-30T07:00:00Z" },
    } }
  elseif args.folder == "empty" then
    return { ok = true, messages = {} }
  end
  return { ok = false, error = "not_found", message = "no such folder" }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"folder": "inbox"},
                "expected": {
                    "count": 3,
                    "senders": ["ana@x.com", "bob@y.com"],
                    "latest_subject": "Deploy done",
                },
            },
            {
                "input": {"folder": "empty"},
                "expected": {"count": 0, "senders": [], "latest_subject": None},
            },
            {"input": {"folder": "spam"}, "expected": {"error": "not_found"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.email.list_unread({ folder = input.folder })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local seen: { [string]: boolean } = {}
    local senders: { string } = {}
    local latest: Email? = nil
    for _, m in r.messages do
        if not seen[m.from] then
            seen[m.from] = true
            table.insert(senders, m.from)
        end
        if latest == nil or m.received_at > latest.received_at then
            latest = m
        end
    end
    table.sort(senders)
    return {
        count = #r.messages,
        senders = senders,
        latest_subject = if latest then latest.subject else nil,
    }
end
""",
    },
]
