SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    IF to_regclass('"LiteLLM_AccountingDelivery"') IS NOT NULL THEN
        LOCK TABLE "LiteLLM_AccountingDelivery" IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM "LiteLLM_AccountingDelivery" WHERE state <> 'applied') THEN
            RAISE EXCEPTION 'LiteLLM_AccountingDelivery has pending or parked rows from the experimental accounting_capture setting. Drain them on the previous build with that setting still on before upgrading';
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS "LiteLLM_AccountingDelivery";
DROP TABLE IF EXISTS "LiteLLM_AccountingEvent";
DROP TYPE IF EXISTS "AccountingDeliveryTarget";
DROP TYPE IF EXISTS "AccountingEventKind";
