do $$
begin
    if exists (
        select 1 from summary_queue where body_enc is null and coalesce(body, '') <> ''
    ) then
        raise exception 'summary_queue still holds plaintext bodies';
    end if;
    if exists (select 1 from document_summaries where summary is not null) then
        raise exception 'document_summaries still holds plaintext summaries';
    end if;
end
$$;

delete from document_summaries s
using summary_queue q
where q.document_id = s.document_id
  and q.body_enc is null
  and s.event_seq <= q.event_seq;

delete from summary_queue where body_enc is null;

alter table summary_queue
    drop constraint ck_summary_queue_body_key,
    drop column body,
    alter column body_enc set not null,
    alter column wrapped_key set not null,
    add constraint ck_summary_queue_body_enc check (
        octet_length(body_enc) >= 29
        and substring(body_enc from 1 for 1) = '\x01'::bytea
        and octet_length(wrapped_key) > 0
    );

alter table document_summaries
    drop constraint ck_document_summaries_summary,
    drop column summary,
    alter column summary_enc set not null,
    alter column wrapped_key set not null,
    add constraint ck_document_summaries_summary_enc check (
        octet_length(summary_enc) >= 29
        and substring(summary_enc from 1 for 1) = '\x01'::bytea
        and octet_length(wrapped_key) > 0
    );
