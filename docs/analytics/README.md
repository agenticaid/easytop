# Usage analytics: content-free, Google Cloud only

"I can see how the product is used, what it costs and where it breaks, without ever seeing the kids' data."
Spec: Obsidian vault, `Spec/07 Errors, Testing, Deployment.md` → Usage analytics. Code: `src/easytop/analytics.py`.

```
bot (Cloud Run) --JSON log lines (stderr)--> Cloud Logging --sink: only lines with an `event` key--> BigQuery
   easytop_analytics_<env>.run_googleapis_com_stderr  (+ synthetic_events in dev)
   -> views in this folder -> the Streamlit page (tools/dashboard, runs on your Mac)
                           -> or Looker Studio (click guide below, not built)
```

What is recorded: enums, counts, durations, tokens, cost, exception class names and a pseudonymous `parent_ref`
(HMAC of the Telegram id, keyed by the environment's session key). Never text, names, ids, cookies or phone numbers;
`tests/test_privacy_end_to_end.py` proves it.

## Set up (once per environment)

    tools/ops/analytics_setup.sh your-gcp-project me-west1 dev     # dataset, synthetic table (dev), sink, writer access
    uv run python -m tools.analytics.synthetic --load your-gcp-project   # dev only: SYNTHETIC demo events (60 days, seed 1)
    tools/ops/analytics_views.sh your-gcp-project dev              # views (run again once the first real event arrived)

For prod the same with `prod` (no synthetic table). Re-running any of them is safe.

## Views

| View | Used for |
|---|---|
| `events` | every event, typed; the base of the others and of the headline tiles |
| `daily_usage` | active parents, questions, cost per day and per question, cache share |
| `funnel` | invite → redeemed → connect shown → opened → signed in → kids confirmed → first question → came back |
| `question_mix` | category, focus, one kid / all, language, follow-ups |
| `latency_daily` | answer time p50 / p95, tool calls per answer |
| `tools_per_question` | tool-call count histogram, which tools |
| `outcomes_daily` | answered / no data / expired / error / truncated / tool limit / rate limited |
| `errors_daily` | failures by place and exception type |
| `session_lifetime` | how long a Webtop login lives, by sign-in route (ended or still alive) |
| `nudges_daily` | evening nudges sent / no school / expired and the settings chosen |
| `periods` | helper: the last 7 / 28 / 90 complete days (Israel dates, ending yesterday) and the same number of days before them |
| `tomorrow_coverage` | the north star: share of school evenings (Sunday-Thursday) on which an active family asked about tomorrow, per period |
| `habit_weekly` | per week: active families and how many were active 1, 2, 3, 4, 5+ days |
| `week2_return` | of the families whose first answer is 14+ days old, how many asked again 7-13 days later |
| `time_to_first_answer` | median minutes from the first connect button to the first answer (counts only) |
| `reconnects_weekly` | per week: "connection expired" buttons shown, active families, reconnects per family |
| `worth_it` | per period: questions, cost per family per month, exact p95 answer time, failure and no-data shares |
| `nudge_conversion` | per day: nudges sent and how many were followed by a question within 30 minutes |
| `session_lifetime_by_route` | `session_lifetime` as counts and medians per route and state (the per-login view holds pseudonyms, so only this one is read by the page) |

## Looker Studio, click by click (Erez, about 15 minutes)

Sign in to https://lookerstudio.google.com with the Google account that owns the project.

1. **Create → Report.** In "Add data to report" choose **BigQuery** and authorise once.
2. **My projects → your-gcp-project → easytop_analytics_dev → `daily_usage` → Add → Add to report.**
3. Add the other sources the same way: **Resource → Manage added data sources → Add a data source → BigQuery**, one
   per view: `events`, `funnel`, `question_mix`, `latency_daily`, `tools_per_question`, `outcomes_daily`,
   `errors_daily`, `session_lifetime`.
4. **Filters for the whole report:** Insert → **Drop-down list** control, data source `events`, control field
   `synthetic` (default: show both while demoing synthetic data; untick `true` for real usage). Add a second one for
   `env`. Add **Insert → Date range control** (default: last 28 days).
5. **Page 1, headline tiles** (Insert → Scorecard, source `events`):
   - Active parents: metric `parent_ref`, aggregation **Count Distinct**, filter `event = question`.
   - Questions: metric **Record Count**, filter `event = question` and `outcome != rate_limited`.
   - Cost per question: source `daily_usage`, metric `cost_per_question_usd`, aggregation **Average**.
   - p95 answer time: source `latency_daily`, metric `p95_seconds`, aggregation **Max**.
6. **Funnel:** Insert → Bar chart, source `funnel`, dimension `step`, metric `n` (Sum), sort by `step` ascending.
7. **What parents ask:** Insert → Pie chart, source `question_mix`, dimension `category`, metric `questions`; a
   second pie with dimension `focus` filtered to `category = tomorrow`; a bar with `language`.
8. **Cost:** Insert → Time series, source `daily_usage`, dimension `day`, metrics `cost_usd` and
   `cost_per_question_usd` (right axis).
9. **Latency:** Time series, source `latency_daily`, dimension `day`, metrics `p50_seconds`, `p95_seconds`; a bar
   from `tools_per_question`, dimension `tool_calls`, metric `n`.
10. **Where it breaks:** stacked bar, source `outcomes_daily`, dimension `day`, breakdown `outcome`, metric
    `questions`; a table from `errors_daily` (`place`, `error_type`, `n`).
11. **Session lifetime:** bar chart, source `session_lifetime`, dimension `route`, breakdown `state`, metric
    `lifetime_hours` (**Average**; add a second with **Max**). Note on the chart: "alive = still valid, a lower bound".
12. **Share:** keep the report private, or Share → "Anyone with the link can view" only for the demo: viewers then
    see the dashboard (aggregates and pseudonyms), never BigQuery itself.

Text for the closing slide of the demo: the schema table in the spec ("everything we record").

## The Streamlit page (tools/dashboard), Spec 12

A one-page dashboard in plain sentences: a headline (tomorrow-evening coverage), then Habit, Activation, Friction
and Worth it, each with one sentence and one or two charts. It runs on your Mac with your own Google login; nothing
is hosted.

    gcloud auth application-default login                       # once, with the project's Google account
    uv run --extra dashboard streamlit run tools/dashboard/app.py
    EASYTOP_DASHBOARD_SOURCE=snapshot uv run --extra dashboard streamlit run tools/dashboard/app.py   # offline

- Sidebar: environment (prod by default; prod shows "No prod data yet" until it is deployed and the setup has run),
  period (7 / 28 / 90 days, complete days ending yesterday), and in dev "Show the SYNTHETIC demo data" (real and
  made-up numbers are never added together).
- Five queries in all, one per part of the page, each with BigQuery query parameters (`@start`, `@end`, `@env`,
  `@synthetic`) and reading only the aggregate views. Under each part a grey line from the job's own stats ("scanned
  590 KB in 1.1 s", or "from cache" within 10 minutes of a reload).
- The page never shows a pseudonym or a per-family row; the footer lists the events from `easytop.analytics.SCHEMA`.
- Sentences: `tools/dashboard/insights.py` (below 5 families it says counts; a change against the previous period is
  stated only with 20+ questions in both and a move over 10%, otherwise "too little data to compare yet").
- Offline run and tests use `tools/dashboard/snapshot/*.csv`, the five queries' results for the synthetic dev data.
  Refresh them after loading new synthetic data: `uv run --extra dashboard python -m tools.dashboard.make_snapshot`.
- Tests: `uv run --extra dashboard pytest tests/dashboard` (they skip without the extra). The views against real
  BigQuery, compared with the same numbers computed in Python from the generator's events (it replaces the
  SYNTHETIC table in dev): `EASYTOP_LIVE_BQ=1 uv run pytest tests/test_analytics_views_live.py`.
- The dashboard libraries are an optional extra, so the bot's Docker image does not grow.
