create table chat_usage (
    turn_id uuid primary key,
    user_id uuid not null,
    usage_date date not null,
    credits bigint not null check (credits >= 0),
    created_at timestamptz not null default now()
);

create index chat_usage_user_date on chat_usage (user_id, usage_date);
