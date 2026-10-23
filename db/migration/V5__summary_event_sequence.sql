-- ce_sequence of the content event a row came from. Summaries and queued changes
-- are ordered by it; revision_no stays only to show which revision a summary was
-- written from. Rows written before this start at 0, before any event.
alter table summary_queue add column event_seq bigint not null default 0;
alter table document_summaries add column event_seq bigint not null default 0;
