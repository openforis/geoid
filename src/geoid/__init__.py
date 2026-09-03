"""GeoID — a global, federated registry that mints immutable, secure identifiers
for geospatial places and serves them over an OGC API Features read surface.

The product's value is the *write path*: mint an immutable geoid (a content-addressed
UUIDv8 derived from the canonical geometry hash), deduplicate by that same hash, attach
provenance, and accept anonymous contributions. The read path is standard OGC API
Features over PostGIS.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version in pyproject.toml, read from the installed
    # package metadata so it is never hand-maintained here (and never drifts, the way
    # the old hardcoded literal silently lagged the released tag).
    __version__ = version("geoid")
except PackageNotFoundError:  # uninstalled source tree — no dist metadata to read
    __version__ = "0.0.0+unknown"
