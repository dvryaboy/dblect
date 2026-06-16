{% snapshot orders_snapshot %}
{{
    config(
        target_schema='snapshots',
        unique_key='id',
        strategy='check',
        check_cols=['status'],
    )
}}
select id, user_id, order_date, status from {{ ref('raw_orders') }}
{% endsnapshot %}
