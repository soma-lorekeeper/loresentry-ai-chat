alter table summary_queue
    alter column body drop not null,
    alter column body set default '',
    add column body_enc bytea,
    add column wrapped_key bytea,
    add constraint ck_summary_queue_body_key
        check ((body_enc is null) = (wrapped_key is null));
