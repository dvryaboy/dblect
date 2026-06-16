{% snapshot orders_snapshot_renamed %}
{{
    config(
        target_schema='snapshots',
        unique_key='id',
        strategy='check',
        check_cols=['status'],
        snapshot_meta_column_names={
            'dbt_valid_from': 'valid_from',
            'dbt_valid_to': 'valid_to',
            'dbt_scd_id': 'scd_id',
            'dbt_updated_at': 'updated_at',
        },
    )
}}
select id, user_id, order_date, status from {{ ref('raw_orders') }}
{% endsnapshot %}
