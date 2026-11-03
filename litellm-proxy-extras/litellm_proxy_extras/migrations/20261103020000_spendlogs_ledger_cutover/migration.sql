SET LOCAL lock_timeout = '10s';
ALTER TABLE "LiteLLM_SpendLogDelivery" ADD COLUMN IF NOT EXISTS "legacy_takeover" BOOLEAN NOT NULL DEFAULT false;
