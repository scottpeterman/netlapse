"""
Netlapse DCIM — NetBox-aligned device inventory.

Ported from VelocityCollector with adaptations for the
headless daemon model (persistent jobs, collection state tracking).
"""

from netlapse.dcim.db_schema import (
    NetlapseDB,
    init_database,
    SCHEMA_VERSION,
)

__all__ = ["NetlapseDB", "init_database", "SCHEMA_VERSION"]
