-- Lead-run only: psql -X -qAt -v ON_ERROR_STOP=1 -f telemetry.sql > telemetry.json
-- Content-free aggregates only. No tenant/user/channel/session/event IDs are emitted.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';
SET LOCAL TIME ZONE 'UTC';
-- BEGIN QUERY
WITH bounds AS (
    SELECT CURRENT_TIMESTAMP - INTERVAL '14 days' AS window_start,
           CURRENT_TIMESTAMP AS window_end
), usage_by_model AS (
    SELECT COALESCE(NULLIF(u.model, ''), 'unknown') AS model,
           COUNT(*) AS usage_events,
           SUM(u.input_tokens)::bigint AS uncached_input_tokens,
           SUM(u.cache_read_input_tokens)::bigint AS cache_read_input_tokens,
           SUM(u.cache_creation_input_tokens)::bigint AS cache_write_input_tokens,
           SUM(u.output_tokens)::bigint AS output_tokens
    FROM public.usage_events u CROSS JOIN bounds b
    WHERE u.occurred_at >= b.window_start AND u.occurred_at < b.window_end
    GROUP BY COALESCE(NULLIF(u.model, ''), 'unknown')
), turn_models AS (
    -- Dedup repeated model IDs before computing latency; do not weight a turn
    -- by its model calls or by a usage_events join. Multi-model turns are
    -- counted once in each model cohort; duration is whole-turn latency.
    SELECT DISTINCT t.id, t.duration_ms,
           COALESCE(NULLIF(m.model, ''), 'unknown') AS model
    FROM public.turn_outcomes t CROSS JOIN bounds b
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(t.model_ids) = 'array' THEN t.model_ids ELSE '[]'::jsonb END
    ) AS m(model)
    WHERE t.started_at >= b.window_start AND t.started_at < b.window_end
), latency_by_model AS (
    SELECT model, COUNT(*) AS turns,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_total_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_total_ms
    FROM turn_models
    GROUP BY model
), model_rows AS (
    SELECT COALESCE(u.model, l.model) AS model,
           COALESCE(u.usage_events, 0) AS usage_events,
           u.uncached_input_tokens AS uncached_input_tokens,
           u.cache_read_input_tokens AS cache_read_input_tokens,
           u.cache_write_input_tokens AS cache_write_input_tokens,
           u.output_tokens AS output_tokens,
           COALESCE(l.turns, 0) AS turns, l.p50_total_ms, l.p95_total_ms
    FROM usage_by_model u FULL OUTER JOIN latency_by_model l USING (model)
)
SELECT jsonb_build_object(
    'schema_version', 1,
    'provider', 'anthropic',
    'window_start', b.window_start,
    'window_end', b.window_end,
    'token_source', 'usage_events: metered tenant calls only; input buckets are disjoint',
    'latency_source', 'SYS-066 turn_outcomes: whole-turn duration per distinct model cohort',
    'first_token_status', 'unavailable: no first-token timestamp in current telemetry schema',
    'models', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'model', r.model, 'usage_events', r.usage_events, 'turns', r.turns,
        'uncached_input_tokens', r.uncached_input_tokens,
        'cache_read_input_tokens', r.cache_read_input_tokens,
        'cache_write_input_tokens', r.cache_write_input_tokens,
        'output_tokens', r.output_tokens,
        'p50_total_ms', r.p50_total_ms, 'p95_total_ms', r.p95_total_ms,
        'p50_first_token_ms', NULL, 'p95_first_token_ms', NULL
    ) ORDER BY r.model) FROM model_rows r), '[]'::jsonb)
) FROM bounds b;
-- END QUERY
COMMIT;
