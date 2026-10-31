-- Telegram channel: one forum group; each conversation is a topic, each decision card is one bot message.
CREATE TABLE telegram_topics (
    conversation_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, chat_id)
);
CREATE UNIQUE INDEX idx_telegram_thread ON telegram_topics(chat_id, thread_id);
CREATE TABLE telegram_cards (
    interaction_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (interaction_id, chat_id)
);
CREATE INDEX idx_telegram_card_msg ON telegram_cards(chat_id, message_id);
CREATE TABLE telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
