"""JSONLogic shape checks matching Trama's standard JSONLogic evaluator."""

OPERATORS = frozenset(
    [
        "var",
        "missing",
        "missing_some",
        "if",
        "?:",
        "==",
        "===",
        "!=",
        "!==",
        ">",
        ">=",
        "<",
        "<=",
        "!",
        "!!",
        "and",
        "or",
        "in",
        "cat",
        "substr",
        "+",
        "-",
        "*",
        "/",
        "%",
        "min",
        "max",
        "merge",
        "map",
        "filter",
        "reduce",
        "all",
        "none",
        "some",
        "log",
    ]
)


def expression_errors(expression, location="condition"):
    errors = []
    if isinstance(expression, list):
        for index, value in enumerate(expression):
            errors.extend(expression_errors(value, f"{location}[{index}]"))
    elif isinstance(expression, dict):
        if len(expression) != 1:
            return [f"{location}: JSONLogic requires exactly one operator"]
        operator, operands = next(iter(expression.items()))
        if operator not in OPERATORS:
            return [f"{location}: unsupported JSONLogic operator '{operator}'"]
        errors.extend(expression_errors(operands, f"{location}.{operator}"))
    return errors
