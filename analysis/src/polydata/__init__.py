"""Analysis toolkit for the polytrader recorder data (DuckDB over Parquet).

    import polydata
    con = polydata.connect()          # every stream as a view, plus `markets`, `chainlink`, ...
    con.sql("FROM markets LIMIT 5").show()
"""

from .config import paths
from .views import connect, create_views

__all__ = ["connect", "create_views", "paths"]
