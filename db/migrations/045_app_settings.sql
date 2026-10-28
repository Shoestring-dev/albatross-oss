-- Keys and settings saved from the Setup page (albatross/keys.py). A value set in the environment (.env, data.keys)
-- wins over the one here. The dashboard never sends a saved value back to the browser.
CREATE TABLE IF NOT EXISTS app_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
