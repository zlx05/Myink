"""Database URL normalization shared by SQLAlchemy and psycopg pools."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_localhost_database_url(value: str) -> str:
    """Use an explicit IPv4 loopback for Windows background connection workers."""
    parsed = urlsplit(value)
    if parsed.hostname != "localhost":
        return value
    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{userinfo}127.0.0.1{port}", parsed.path,
                       parsed.query, parsed.fragment))
