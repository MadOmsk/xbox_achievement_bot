CREATE TABLE IF NOT EXISTS daily_reports (
    chat_id     INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (chat_id, report_date)
);
