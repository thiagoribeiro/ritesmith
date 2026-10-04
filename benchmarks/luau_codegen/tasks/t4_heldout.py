"""T4 — held-out set: scenarios NOT used while tuning the prompts.

Kept separate from T1–T3 so pass rates here measure generalisation, not prompt
overfitting. Same shape and invariants as the other tiers; selftest proves every
reference compiles, strict-typechecks and passes its cases on both targets.
"""

TOOL_ERROR = "type ToolError = { ok: false, error: string, message: string }"

TASKS: list[dict] = [
    {
        "id": "t4_roman_numeral",
        "goal": (
            "Convert an integer in 1..3999 to its Roman numeral string as {roman}. If the "
            "number is outside that range, return {error = 'out_of_range', message = <any text>}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer"}},
            "required": ["number"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "roman": {"type": "string"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": """
type Input = { number: number }
type Output = { roman: string } | { error: string, message: string }
""",
        "test_cases": [
            {"input": {"number": 1}, "expected": {"roman": "I"}},
            {"input": {"number": 4}, "expected": {"roman": "IV"}},
            {"input": {"number": 2026}, "expected": {"roman": "MMXXVI"}},
            {"input": {"number": 3999}, "expected": {"roman": "MMMCMXCIX"}},
            {"input": {"number": 0}, "expected": {"error": "out_of_range"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local n = input.number
    if n < 1 or n > 3999 then
        return { error = "out_of_range", message = "number must be 1..3999" }
    end
    local vals = { 1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1 }
    local syms = { "M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I" }
    local out = ""
    for i = 1, #vals do
        while n >= vals[i] do
            out ..= syms[i]
            n -= vals[i]
        end
    end
    return { roman = out }
end
""",
    },
    {
        "id": "t4_median",
        "goal": (
            "Return the median of a list of numbers as {median}, rounded to 2 decimals (for an "
            "even count, the average of the two middle values). Empty list → "
            "{error = 'empty', message = <any text>}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"values": {"type": "array", "items": {"type": "number"}}},
            "required": ["values"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "median": {"type": "number"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": """
type Input = { values: { number } }
type Output = { median: number } | { error: string, message: string }
""",
        "test_cases": [
            {"input": {"values": [3, 1, 2]}, "expected": {"median": 2}},
            {"input": {"values": [1, 2, 3, 4]}, "expected": {"median": 2.5}},
            {"input": {"values": [5]}, "expected": {"median": 5}},
            {"input": {"values": [10, 2, 38, 23, 23, 38, 21]}, "expected": {"median": 23}},
            {"input": {"values": []}, "expected": {"error": "empty"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local values = input.values
    if #values == 0 then
        return { error = "empty", message = "no values" }
    end
    local sorted: { number } = {}
    for _, v in values do
        table.insert(sorted, v)
    end
    table.sort(sorted)
    local n = #sorted
    local mid = n // 2
    local m: number
    if n % 2 == 1 then
        m = sorted[mid + 1]
    else
        m = (sorted[mid] + sorted[mid + 1]) / 2
    end
    return { median = math.floor(m * 100 + 0.5) / 100 }
end
""",
    },
    {
        "id": "t4_rle_encode",
        "goal": (
            "Run-length encode a string: return {runs}, a list of {char, count} for each maximal "
            "run of identical characters, left to right. Empty string → empty list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "runs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"char": {"type": "string"}, "count": {"type": "integer"}},
                    },
                }
            },
            "required": ["runs"],
        },
        "types": """
type Run = { char: string, count: number }
type Input = { text: string }
type Output = { runs: { Run } }
""",
        "test_cases": [
            {
                "input": {"text": "aaabbc"},
                "expected": {
                    "runs": [
                        {"char": "a", "count": 3},
                        {"char": "b", "count": 2},
                        {"char": "c", "count": 1},
                    ]
                },
            },
            {"input": {"text": ""}, "expected": {"runs": []}},
            {"input": {"text": "x"}, "expected": {"runs": [{"char": "x", "count": 1}]}},
            {
                "input": {"text": "aAaa"},
                "expected": {
                    "runs": [
                        {"char": "a", "count": 1},
                        {"char": "A", "count": 1},
                        {"char": "a", "count": 2},
                    ]
                },
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local runs: { Run } = {}
    local text = input.text
    local len = #text
    local i = 1
    while i <= len do
        local ch = string.sub(text, i, i)
        local count = 1
        while i + count <= len and string.sub(text, i + count, i + count) == ch do
            count += 1
        end
        table.insert(runs, { char = ch, count = count })
        i += count
    end
    return { runs = runs }
end
""",
    },
    {
        "id": "t4_balanced_brackets",
        "goal": (
            "Return {balanced} = true iff every (), [] and {} in the text is correctly matched "
            "and nested. Other characters are ignored."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"balanced": {"type": "boolean"}},
            "required": ["balanced"],
        },
        "types": """
type Input = { text: string }
type Output = { balanced: boolean }
""",
        "test_cases": [
            {"input": {"text": "a(b[c]{d})e"}, "expected": {"balanced": True}},
            {"input": {"text": "(]"}, "expected": {"balanced": False}},
            {"input": {"text": "(()"}, "expected": {"balanced": False}},
            {"input": {"text": ""}, "expected": {"balanced": True}},
            {"input": {"text": "{[()]}"}, "expected": {"balanced": True}},
            {"input": {"text": ")("}, "expected": {"balanced": False}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local closers: { [string]: string } = { [")"] = "(", ["]"] = "[", ["}"] = "{" }
    local openers: { [string]: boolean } = { ["("] = true, ["["] = true, ["{"] = true }
    local stack: { string } = {}
    local text = input.text
    for i = 1, #text do
        local ch = string.sub(text, i, i)
        if openers[ch] then
            table.insert(stack, ch)
        elseif closers[ch] ~= nil then
            if #stack == 0 or stack[#stack] ~= closers[ch] then
                return { balanced = false }
            end
            table.remove(stack)
        end
    end
    return { balanced = #stack == 0 }
end
""",
    },
    {
        "id": "t4_caesar_cipher",
        "goal": (
            "Apply a Caesar cipher: shift each ASCII letter by `shift` positions (wrapping within "
            "its case), leaving every non-letter unchanged. `shift` may be negative. Return {text}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "shift": {"type": "integer"}},
            "required": ["text", "shift"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "types": """
type Input = { text: string, shift: number }
type Output = { text: string }
""",
        "test_cases": [
            {"input": {"text": "abc", "shift": 1}, "expected": {"text": "bcd"}},
            {"input": {"text": "xyz", "shift": 3}, "expected": {"text": "abc"}},
            {
                "input": {"text": "Hello, World!", "shift": 13},
                "expected": {"text": "Uryyb, Jbeyq!"},
            },
            {"input": {"text": "abc", "shift": 0}, "expected": {"text": "abc"}},
            {"input": {"text": "bcd", "shift": -1}, "expected": {"text": "abc"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local shift = input.shift % 26
    local out: { string } = {}
    local text = input.text
    for i = 1, #text do
        local b = string.byte(text, i) :: number
        if b >= 65 and b <= 90 then
            b = (b - 65 + shift) % 26 + 65
        elseif b >= 97 and b <= 122 then
            b = (b - 97 + shift) % 26 + 97
        end
        table.insert(out, string.char(b))
    end
    return { text = table.concat(out) }
end
""",
    },
    {
        "id": "t4_inventory_reorder",
        "goal": (
            "Look up stock for `sku` and return {sku, quantity, reorder}. reorder is 0 when "
            "quantity >= threshold, otherwise threshold*2 - quantity. If the tool fails, return "
            "{error, message} from the tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "threshold": {"type": "integer"}},
            "required": ["sku", "threshold"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer"},
                "reorder": {"type": "integer"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": f"""
{TOOL_ERROR}
type StockResult = {{ ok: true, sku: string, quantity: number }} | ToolError
type Input = {{ sku: string, threshold: number }}
type Output = {{ sku: string, quantity: number, reorder: number }}
    | {{ error: string, message: string }}
""",
        "tools": [
            {
                "name": "warehouse.stock",
                "signature": "(args: { sku: string }) -> StockResult",
                "description": (
                    "args {sku}. Returns {ok = true, sku, quantity}. On failure returns "
                    "{ok = false, error = <code>, message = <text>}."
                ),
                "stub": """
function(args)
  local stock = { A1 = 3, B2 = 50, C3 = 0 }
  local q = stock[tostring(args.sku)]
  if q == nil then
    return { ok = false, error = "unknown_sku", message = "no such sku" }
  end
  return { ok = true, sku = args.sku, quantity = q }
end
""",
            }
        ],
        "test_cases": [
            {
                "input": {"sku": "A1", "threshold": 10},
                "expected": {"sku": "A1", "quantity": 3, "reorder": 17},
                "expected_calls": {"warehouse.stock": 1},
            },
            {
                "input": {"sku": "B2", "threshold": 10},
                "expected": {"sku": "B2", "quantity": 50, "reorder": 0},
            },
            {
                "input": {"sku": "C3", "threshold": 5},
                "expected": {"sku": "C3", "quantity": 0, "reorder": 10},
            },
            {"input": {"sku": "Z9", "threshold": 5}, "expected": {"error": "unknown_sku"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local r = tools.warehouse.stock({ sku = input.sku })
    if not r.ok then
        return { error = r.error, message = r.message }
    end
    local reorder = if r.quantity < input.threshold then input.threshold * 2 - r.quantity else 0
    return { sku = r.sku, quantity = r.quantity, reorder = reorder }
end
""",
    },
]
