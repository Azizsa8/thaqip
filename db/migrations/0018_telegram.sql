-- 0018: link a tenant's users to Telegram chats without anyone typing a chat id.
--
-- The console hands out a one-time deep link (t.me/<bot>?start=<code>); the
-- bot sees "/start <code>" and records the chat. Only the sha256 of the code
-- is stored, it expires in minutes, and it can be used once.

CREATE TABLE IF NOT EXISTS telegram_link_codes (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   bigint NOT NULL REFERENCES tenants(id),
  user_id     bigint REFERENCES users(id) ON DELETE CASCADE,
  code_hash   text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);

CREATE TABLE IF NOT EXISTS telegram_links (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    bigint NOT NULL REFERENCES tenants(id),
  user_id      bigint REFERENCES users(id) ON DELETE SET NULL,
  chat_id      text NOT NULL,
  chat_type    text NOT NULL DEFAULT 'private',
  chat_title   text,
  tg_username  text,
  first_name   text,
  active       boolean NOT NULL DEFAULT true,
  linked_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, chat_id)
);
CREATE INDEX IF NOT EXISTS telegram_links_live ON telegram_links (tenant_id) WHERE active;
