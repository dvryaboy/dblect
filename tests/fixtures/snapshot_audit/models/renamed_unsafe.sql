-- Reads a snapshot whose validity columns were renamed via
-- snapshot_meta_column_names, with no temporal filter. The detector should flag
-- it (the relation is still a snapshot regardless of column naming).
select id, status
from {{ ref('orders_snapshot_renamed') }}
