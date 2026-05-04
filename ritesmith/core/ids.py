from ulid import ULID


def generate_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"
