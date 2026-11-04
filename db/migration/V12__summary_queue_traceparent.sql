-- W3C traceparent of the content event a queued change came from. The summary job
-- logs under that trace, so the summary shows at the end of the edit's trace. Rows
-- queued before this, and events without the header, have none.
alter table summary_queue add column traceparent varchar(55);
