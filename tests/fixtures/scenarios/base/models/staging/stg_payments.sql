with source as (

    select * from {{ ref('raw_payments') }}

),

renamed as (

    select
        id as payment_id,
        order_id,
        payment_method,

        -- `amount` is stored in cents; convert to the major unit. The currency
        -- it is denominated in travels alongside it.
        amount / 100 as amount,
        currency

    from source

)

select * from renamed
