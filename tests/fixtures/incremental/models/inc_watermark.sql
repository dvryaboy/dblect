{{ config(materialized='incremental', unique_key='id') }}

select id, event_time, amount
from {{ ref('events') }}
{% if is_incremental() %}
where event_time > (select max(event_time) from {{ this }})
{% endif %}
