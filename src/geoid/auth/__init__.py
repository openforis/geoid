"""Authentication helpers (OIDC claim mapping + JWT validation).

Kept separate from :mod:`geoid.deps` (the FastAPI dependency seam) so the pure,
network-free logic is unit-testable in isolation and so an image without the extra
that never imports this package still imports ``deps.py`` without PyJWT installed.
"""
