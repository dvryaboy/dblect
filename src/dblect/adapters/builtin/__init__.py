"""Built-in adapter profiles, one module per warehouse.

Each module builds an :class:`~dblect.adapters.model.AdapterProfile` and calls
:func:`~dblect.adapters.registry.register`. The registry discovers every module in
this package automatically, so adding a warehouse is a new file here and nothing
else. A new module is also the place to record, in its own comments, why that
adapter enforces what it does.
"""
