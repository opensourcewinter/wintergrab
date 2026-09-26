"""Storage adapters: where items go besides JSON Lines, JSON, CSV and SQLite, and where they are read from.

========================  =====================================  ==============================
Output                    What                                   Needs
========================  =====================================  ==============================
``items.parquet``         A Parquet file, a typed column a field  ``pip install "wintergrab[parquet]"``
``items.xlsx``            An Excel workbook, a column a field     ``pip install "wintergrab[xlsx]"``
``postgresql://.../db``   Rows of a PostgreSQL table, upserted    ``pip install "wintergrab[postgres]"``
                          on ``unique_key``
========================  =====================================  ==============================

A spider writes to them like to any output (``output = "items.parquet"``, ``crawl ... -o
postgresql://crawler@db/shop?table=products``), and :func:`wintergrab.data.io.read_records` reads
them back (``wintergrab data validate items.parquet``). None of them is needed by anything else:
without its library, only that output says what to install.

Other formats plug in with :func:`wintergrab.spider.exporters.register_exporter` and
:func:`wintergrab.data.io.register_reader`.
"""

from __future__ import annotations

__all__: list[str] = []
