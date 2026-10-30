alter table document_summaries
    alter column summary drop not null,
    add column summary_enc bytea,
    add column wrapped_key bytea,
    add constraint ck_document_summaries_summary check (
        (summary is not null and summary_enc is null and wrapped_key is null)
        or (summary is null and summary_enc is not null and wrapped_key is not null)
    );
