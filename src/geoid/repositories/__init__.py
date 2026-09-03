"""Repository layer — the only place that talks SQL.

Business logic (services) depends on these interfaces, not on the storage
mechanism, which keeps the write hot path (dedup) and the OGC read path testable
and swappable.
"""
