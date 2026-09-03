"""Service layer — orchestration of domain + repositories.

``registry_service`` is the product: mint an immutable geoid, deduplicate by
canonical geometry, attach provenance, and honour the anonymous-write data
policy. ``ogc_service`` assembles OGC API Features responses. ``listing_service``
backs the 1.2 management slice. ``bootstrap`` guarantees the reserved anonymous
collection exists.
"""
