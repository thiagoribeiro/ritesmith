"""T1 — pure transformations: no tools, only deterministic logic."""

TASKS: list[dict] = [
    {
        "id": "t1_celsius_to_fahrenheit",
        "goal": "Convert a temperature from Celsius to Fahrenheit.",
        "input_schema": {
            "type": "object",
            "properties": {"celsius": {"type": "number"}},
            "required": ["celsius"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"fahrenheit": {"type": "number"}},
            "required": ["fahrenheit"],
        },
        "types": """
type Input = { celsius: number }
type Output = { fahrenheit: number }
""",
        "test_cases": [
            {"input": {"celsius": 0}, "expected": {"fahrenheit": 32}},
            {"input": {"celsius": 100}, "expected": {"fahrenheit": 212}},
            {"input": {"celsius": -40}, "expected": {"fahrenheit": -40}},
            {"input": {"celsius": 37}, "expected": {"fahrenheit": 98.6}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    return { fahrenheit = input.celsius * 9 / 5 + 32 }
end
""",
    },
    {
        "id": "t1_percent_change",
        "goal": (
            "Compute the percentage change from initial_value to current_value, rounded to 2 "
            "decimal places, and a direction: 'up', 'down' or 'flat'. If initial_value is 0, "
            "return {error = 'invalid_initial_value', message = <any text>} instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "initial_value": {"type": "number"},
                "current_value": {"type": "number"},
            },
            "required": ["initial_value", "current_value"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "percentage_change": {"type": "number"},
                "direction": {"enum": ["up", "down", "flat"]},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": """
type Input = { initial_value: number, current_value: number }
type Output = { percentage_change: number, direction: string } | { error: string, message: string }
""",
        "test_cases": [
            {
                "input": {"initial_value": 100, "current_value": 97},
                "expected": {"percentage_change": -3, "direction": "down"},
            },
            {
                "input": {"initial_value": 50000, "current_value": 51234.5},
                "expected": {"percentage_change": 2.47, "direction": "up"},
            },
            {
                "input": {"initial_value": 10, "current_value": 10},
                "expected": {"percentage_change": 0, "direction": "flat"},
            },
            {
                "input": {"initial_value": 0, "current_value": 5},
                "expected": {"error": "invalid_initial_value"},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    if input.initial_value == 0 then
        return { error = "invalid_initial_value", message = "initial_value must not be zero" }
    end
    local change = (input.current_value - input.initial_value) / input.initial_value * 100
    local rounded = math.floor(change * 100 + 0.5) / 100
    local direction = if rounded > 0 then "up" elseif rounded < 0 then "down" else "flat"
    return { percentage_change = rounded, direction = direction }
end
""",
    },
    {
        "id": "t1_slugify",
        "goal": (
            "Turn text into a URL slug: lowercase ASCII, every run of non-alphanumeric "
            "characters becomes a single '-', and no leading or trailing '-'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        "types": """
type Input = { text: string }
type Output = { slug: string }
""",
        "test_cases": [
            {"input": {"text": "Hello, World!"}, "expected": {"slug": "hello-world"}},
            {"input": {"text": "  Lua  vs   Luau 2026 "}, "expected": {"slug": "lua-vs-luau-2026"}},
            {"input": {"text": "---"}, "expected": {"slug": ""}},
            {"input": {"text": "RiteSmith_v2.0"}, "expected": {"slug": "ritesmith-v2-0"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local slug = string.lower(input.text)
    slug = string.gsub(slug, "[^%w]+", "-")
    slug = string.gsub(slug, "^%-+", "")
    slug = string.gsub(slug, "%-+$", "")
    return { slug = slug }
end
""",
    },
    {
        "id": "t1_group_totals",
        "goal": (
            "Sum item amounts per category. Return totals as a map category -> total amount, "
            "and count as the number of items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "category": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                    },
                }
            },
            "required": ["items"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "totals": {"type": "object", "additionalProperties": {"type": "number"}},
                "count": {"type": "integer"},
            },
            "required": ["totals", "count"],
        },
        "types": """
type Item = { name: string, category: string, amount: number }
type Input = { items: { Item } }
type Output = { totals: { [string]: number }, count: number }
""",
        "test_cases": [
            {
                "input": {
                    "items": [
                        {"name": "coffee", "category": "food", "amount": 12.5},
                        {"name": "bus", "category": "transport", "amount": 4.4},
                        {"name": "lunch", "category": "food", "amount": 30},
                        {"name": "metro", "category": "transport", "amount": 4.4},
                        {"name": "book", "category": "education", "amount": 59.9},
                    ]
                },
                "expected": {
                    "totals": {"food": 42.5, "transport": 8.8, "education": 59.9},
                    "count": 5,
                },
            },
            {"input": {"items": []}, "expected": {"totals": {}, "count": 0}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local totals: { [string]: number } = {}
    for _, item in input.items do
        totals[item.category] = (totals[item.category] or 0) + item.amount
    end
    return { totals = totals, count = #input.items }
end
""",
    },
    {
        "id": "t1_dedupe_sort",
        "goal": "Remove duplicate strings and return them sorted ascending (case-sensitive).",
        "input_schema": {
            "type": "object",
            "properties": {"values": {"type": "array", "items": {"type": "string"}}},
            "required": ["values"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"values": {"type": "array", "items": {"type": "string"}}},
            "required": ["values"],
        },
        "types": """
type Input = { values: { string } }
type Output = { values: { string } }
""",
        "test_cases": [
            {
                "input": {"values": ["b", "a", "b", "c", "a"]},
                "expected": {"values": ["a", "b", "c"]},
            },
            {"input": {"values": []}, "expected": {"values": []}},
            {
                "input": {"values": ["beta", "Alpha", "alpha", "beta"]},
                "expected": {"values": ["Alpha", "alpha", "beta"]},
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local seen: { [string]: boolean } = {}
    local out: { string } = {}
    for _, v in input.values do
        if not seen[v] then
            seen[v] = true
            table.insert(out, v)
        end
    end
    table.sort(out)
    return { values = out }
end
""",
    },
    {
        "id": "t1_parse_iso_date",
        "goal": (
            "Parse a date in 'YYYY-MM-DD' format and return year, month, day, day_of_year "
            "(1-based) and is_leap_year. If the string is malformed or the date does not exist "
            "(e.g. month 13, Feb 29 on a non-leap year), return {error = 'invalid_date', "
            "message = <any text>}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "month": {"type": "integer"},
                "day": {"type": "integer"},
                "day_of_year": {"type": "integer"},
                "is_leap_year": {"type": "boolean"},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "types": """
type Input = { date: string }
type Output = {
    year: number, month: number, day: number, day_of_year: number, is_leap_year: boolean,
} | { error: string, message: string }
""",
        "test_cases": [
            {
                "input": {"date": "2026-09-30"},
                "expected": {
                    "year": 2026,
                    "month": 9,
                    "day": 30,
                    "day_of_year": 273,
                    "is_leap_year": False,
                },
            },
            {
                "input": {"date": "2024-03-01"},
                "expected": {
                    "year": 2024,
                    "month": 3,
                    "day": 1,
                    "day_of_year": 61,
                    "is_leap_year": True,
                },
            },
            {"input": {"date": "2026-13-01"}, "expected": {"error": "invalid_date"}},
            {"input": {"date": "2023-02-29"}, "expected": {"error": "invalid_date"}},
            {"input": {"date": "30/09/2026"}, "expected": {"error": "invalid_date"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local ys, ms, ds = string.match(input.date, "^(%d%d%d%d)-(%d%d)-(%d%d)$")
    if not ys then
        return { error = "invalid_date", message = "expected YYYY-MM-DD" }
    end
    local year, month, day = tonumber(ys) :: number, tonumber(ms) :: number, tonumber(ds) :: number
    local leap = (year % 4 == 0 and year % 100 ~= 0) or year % 400 == 0
    local days = { 31, if leap then 29 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }
    if month < 1 or month > 12 or day < 1 or day > days[month] then
        return { error = "invalid_date", message = "date does not exist" }
    end
    local doy = day
    for m = 1, month - 1 do
        doy += days[m]
    end
    return { year = year, month = month, day = day, day_of_year = doy, is_leap_year = leap }
end
""",
    },
    {
        "id": "t1_word_frequency",
        "goal": (
            "Count word frequencies in text (words are runs of ASCII letters, case-insensitive, "
            "lowercased). Return the top_n words ordered by count descending, ties broken "
            "alphabetically ascending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "top_n": {"type": "integer"}},
            "required": ["text", "top_n"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "words": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"word": {"type": "string"}, "count": {"type": "integer"}},
                    },
                }
            },
            "required": ["words"],
        },
        "types": """
type Input = { text: string, top_n: number }
type WordCount = { word: string, count: number }
type Output = { words: { WordCount } }
""",
        "test_cases": [
            {
                "input": {"text": "The cat and the hat and THE bat", "top_n": 3},
                "expected": {
                    "words": [
                        {"word": "the", "count": 3},
                        {"word": "and", "count": 2},
                        {"word": "bat", "count": 1},
                    ]
                },
            },
            {"input": {"text": "", "top_n": 5}, "expected": {"words": []}},
            {
                "input": {"text": "b a c", "top_n": 10},
                "expected": {
                    "words": [
                        {"word": "a", "count": 1},
                        {"word": "b", "count": 1},
                        {"word": "c", "count": 1},
                    ]
                },
            },
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local counts: { [string]: number } = {}
    for word in string.gmatch(string.lower(input.text), "%a+") do
        counts[word] = (counts[word] or 0) + 1
    end
    local list: { WordCount } = {}
    for word, count in counts do
        table.insert(list, { word = word, count = count })
    end
    table.sort(list, function(a: WordCount, b: WordCount)
        if a.count ~= b.count then
            return a.count > b.count
        end
        return a.word < b.word
    end)
    local top: { WordCount } = {}
    for i = 1, math.min(input.top_n, #list) do
        top[i] = list[i]
    end
    return { words = top }
end
""",
    },
    {
        "id": "t1_split_emails",
        "goal": (
            "Split a list of email addresses into valid and invalid, preserving order. Valid: "
            "exactly one '@', non-empty local part, no spaces, and a domain that contains a '.' "
            "that is neither its first nor its last character."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"emails": {"type": "array", "items": {"type": "string"}}},
            "required": ["emails"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "valid": {"type": "array", "items": {"type": "string"}},
                "invalid": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["valid", "invalid"],
        },
        "types": """
type Input = { emails: { string } }
type Output = { valid: { string }, invalid: { string } }
""",
        "test_cases": [
            {
                "input": {
                    "emails": [
                        "a@b.com",
                        "bad",
                        "x@y",
                        "me@site.org",
                        "two@@x.com",
                        "sp ace@x.com",
                        "u@.com",
                        "@x.com",
                    ]
                },
                "expected": {
                    "valid": ["a@b.com", "me@site.org"],
                    "invalid": ["bad", "x@y", "two@@x.com", "sp ace@x.com", "u@.com", "@x.com"],
                },
            },
        ],
        "reference": """
local function isValid(email: string): boolean
    if string.find(email, " ", 1, true) then
        return false
    end
    local parts = string.split(email, "@")
    if #parts ~= 2 or parts[1] == "" then
        return false
    end
    local domain = parts[2]
    local dot = string.find(domain, ".", 1, true)
    return dot ~= nil and string.sub(domain, 1, 1) ~= "." and string.sub(domain, -1) ~= "."
end

function run(input: Input, context: Context): Output
    local valid: { string }, invalid: { string } = {}, {}
    for _, email in input.emails do
        table.insert(if isValid(email) then valid else invalid, email)
    end
    return { valid = valid, invalid = invalid }
end
""",
    },
    {
        "id": "t1_moving_average",
        "goal": (
            "Compute the simple moving average of values with the given window size. Return one "
            "average per full window (length n - window + 1, or empty), each rounded to 2 decimals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}},
                "window": {"type": "integer", "minimum": 1},
            },
            "required": ["values", "window"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"averages": {"type": "array", "items": {"type": "number"}}},
            "required": ["averages"],
        },
        "types": """
type Input = { values: { number }, window: number }
type Output = { averages: { number } }
""",
        "test_cases": [
            {
                "input": {"values": [1, 2, 3, 4, 5], "window": 3},
                "expected": {"averages": [2, 3, 4]},
            },
            {"input": {"values": [10, 20], "window": 3}, "expected": {"averages": []}},
            {
                "input": {"values": [1, 2, 4, 8], "window": 2},
                "expected": {"averages": [1.5, 3, 6]},
            },
            {"input": {"values": [1, 1, 2], "window": 3}, "expected": {"averages": [1.33]}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local out: { number } = {}
    local values, w = input.values, input.window
    for i = 1, #values - w + 1 do
        local sum = 0
        for j = i, i + w - 1 do
            sum += values[j]
        end
        table.insert(out, math.floor(sum / w * 100 + 0.5) / 100)
    end
    return { averages = out }
end
""",
    },
    {
        "id": "t1_format_duration",
        "goal": (
            "Format a non-negative number of seconds as a human-readable duration like "
            "'1d 2h 3m 4s', omitting zero units. Zero seconds is '0s'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "integer", "minimum": 0}},
            "required": ["seconds"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "types": """
type Input = { seconds: number }
type Output = { text: string }
""",
        "test_cases": [
            {"input": {"seconds": 3723}, "expected": {"text": "1h 2m 3s"}},
            {"input": {"seconds": 60}, "expected": {"text": "1m"}},
            {"input": {"seconds": 90061}, "expected": {"text": "1d 1h 1m 1s"}},
            {"input": {"seconds": 0}, "expected": {"text": "0s"}},
            {"input": {"seconds": 172800}, "expected": {"text": "2d"}},
        ],
        "reference": """
function run(input: Input, context: Context): Output
    local remaining = input.seconds
    local parts: { string } = {}
    for _, unit in { { 86400, "d" }, { 3600, "h" }, { 60, "m" }, { 1, "s" } } do
        local size, suffix = unit[1] :: number, unit[2] :: string
        local n = remaining // size
        if n > 0 then
            table.insert(parts, `{n}{suffix}`)
            remaining -= n * size
        end
    end
    return { text = if #parts == 0 then "0s" else table.concat(parts, " ") }
end
""",
    },
]
