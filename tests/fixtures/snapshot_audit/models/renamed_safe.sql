-- Restricts on the snapshot's renamed validity column (`valid_to`, not
-- `dbt_valid_to`). The detector should stay silent only if it reads the validity
-- column names from the snapshot's config rather than assuming the defaults.
select id, status
from {{ ref('orders_snapshot_renamed') }}
where valid_to is null
