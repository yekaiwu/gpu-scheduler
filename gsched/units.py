"""Memory unit conversions. The only place GiB and bytes are converted."""

GiB = 2**30
MiB = 2**20


def gib_to_bytes(x: float) -> int:
    return int(round(x * GiB))


def bytes_to_gib(b: int) -> float:
    return b / GiB


def fmt_gib(b: int | None) -> str:
    if b is None:
        return "?"
    return f"{b / GiB:.1f}GiB"
