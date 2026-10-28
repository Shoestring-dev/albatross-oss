-- events_news (albatross/events/news_extract.py) marks every news item it has sent to the model with that
-- agent_runs id, so each item is read once and each extracted event can be traced to the run that read it.
ALTER TABLE news_items ADD COLUMN events_run_id BIGINT;
