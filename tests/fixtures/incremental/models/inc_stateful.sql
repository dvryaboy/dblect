{{ config(materialized='incremental', unique_key='id') }}

select
    e.id,
    e.event_time,
    e.amount
    {% if is_incremental() %}
    , s.last_seen
    , sum(e.amount) over (partition by e.id) as id_total
    {% endif %}
from {{ ref('events') }} e
{% if is_incremental() %}
left join {{ ref('state') }} s on e.id = s.id
where e.event_time > (select max(event_time) from {{ this }})
{% endif %}
