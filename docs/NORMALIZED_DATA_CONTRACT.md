# Normalized Data Contract

Import adapters should convert each tenant's source files into this contract
before scoring. This keeps the recommender company-agnostic.

## Source Data Boundary

Raw tenant files are read-only source records. Importers must not edit workbooks,
emails, reports, snapshots, or local alias files in place. Derived standardized
tables, validation reports, planning snapshots, and recommendation outputs must
be written only to controlled application workspace, database, or managed storage
locations.

Local raw snapshots belong under ignored paths such as
`RecommenderSystem/local-data/`. Committed fixtures must live under
`RecommenderSystem/fixtures/` and must be synthetic or anonymized.

Real alias maps are tenant/local configuration, not committed sample data. Use
ignored files such as `RecommenderSystem/config/aliases.local.json` or managed
tenant configuration for real aliases. The committed sample alias file under
`RecommenderSystem/fixtures/` is synthetic only.

## Import Audit Metadata

Every import run should produce metadata before its normalized state is scored:

- `import_run_id`: stable ID for the import attempt.
- `imported_at`: timestamp for the import run.
- `source_system`: standardized source label, for example `standardized-csv` or
  a tenant adapter name.
- `source_refs`: source file/object references used by the import.
- `row_counts`: normalized row counts by table.
- `validation_warnings`: structured warnings emitted during validation.
- `generated_state_snapshot_id`: ID for the persisted planning-state snapshot,
  when available.

Every imported row should keep a source reference where practical:

- `import_run_id`
- `table`
- `source_ref`
- `row_number`

## Standardized CSV v1

The first executable import contract is a folder of UTF-8 CSV files plus one
JSON manifest. CSV files use a header row, comma delimiters, RFC 4180 quoting,
and one record per row. Missing optional values are represented by an empty
field, not by placeholder words such as `unknown`, `n/a`, or `none`.

Required files:

- `import_manifest.json`
- `workers.csv`
- `service_users.csv`
- `sessions.csv`
- `availability.csv`
- `unavailability.csv`
- `compatibility.csv`
- `family_requests.csv`
- `pair_history.csv`

Committed examples live under
`RecommenderSystem/fixtures/standardized_csv_v1/` and are synthetic only.

### Shared Formats

IDs:

- IDs are stable tenant-scoped strings, not display names.
- IDs must be non-empty, unique within their table, and stable across import
  runs when the underlying worker, service user, or session has not changed.
- Recommended prefixes are `worker-`, `su-`, `session-`, `request-`, and
  `import-`, but importers must validate uniqueness rather than rely on a
  prefix.
- `import_run_id` comes from `import_manifest.json`. It is copied into each
  row's `SourceReference` during import rather than repeated in every CSV row.

Dates and times:

- Date-time fields use ISO 8601 with an explicit UTC offset, for example
  `2026-07-10T14:00:00+01:00`.
- The offset is the tenant/local operational timezone offset at that instant.
- Naive date-times are invalid.
- End times are exclusive for overlap checks and must be after start times.
- `preferred_days` uses Python weekday integers: `0` Monday through `6`
  Sunday. Order is significant: list days most preferred first. A planner
  provisioning a session from this list tries them in the given order, not
  numeric order, so `5|6` and `6|5` are different requests (Saturday
  preferred vs. Sunday preferred).
- Time ranges use `start/end` ISO 8601 intervals. Lists of intervals are
  pipe-separated.

Lists:

- Multi-value fields use pipe-separated values, for example
  `drive|walk|taxi`.
- Empty optional list fields are empty strings.
- Values are case-sensitive and should be lower snake case unless they are IDs.
- Values inside a list must be unique after trimming whitespace.

Gender:

- `gender` fields use normalized optional values: `female`, `male`,
  `non_binary`, `other`, `prefer_not_to_say`, or `unknown`.
- Gender is sensitive matching context. Importing it does not by itself create a
  hard or soft scoring rule; gender-based constraints or preferences must be
  represented explicitly in future policy/request fields and surfaced for human
  review.
- One such explicit, opt-in policy exists today:
  `TenantRules.restrict_male_workers_from_female_service_users` (off by
  default; enabled via `--restrict-male-worker-female-service-user` on
  `allocate-sessions`/`review-ui`) is a hard constraint that blocks male
  workers from being suggested for female service users. It is a
  tenant-specific convention, not a universal rule, and every session it
  blocks a candidate for still shows the reason in the report.

Booleans and numbers:

- Booleans are `true` or `false`.
- Numeric fields use decimal notation without thousands separators.
- Empty optional numeric fields map to `None`.

Locations:

- Locations are represented as repeated columns:
  `{prefix}_label`, `{prefix}_postcode`, `{prefix}_latitude`,
  `{prefix}_longitude`, and `{prefix}_area`.
- At least one of label, postcode, coordinates, or area should be present. A
  missing usable location is a validation warning unless a tenant rule makes it
  an error.

Ownership:

- `required`: must be present and valid in CSV.
- `optional`: may be empty and maps to a domain default or `None`.
- `derived`: produced by the importer from the file/folder context.
- `future`: documented for roadmap awareness, not part of CSV v1.

### Validation Severities

Validation reports should use these severities before parser code is written:

- `error`: the import cannot safely produce a `PlanningState`.
- `warning`: the import can continue, but recommendations should surface the
  issue.
- `info`: useful audit context that does not affect recommendation safety.

Examples:

- `error`: duplicate worker ID, missing required ID, unknown session
  `service_user_id`, invalid date-time, session end before start, invalid enum
  value.
- `warning`: missing postcode, missing travel mode, missing availability, stale
  service-user profile, low-confidence parsed request, unknown compatibility.
- `info`: optional notes present, empty optional lists, derived source
  references.

### Unknown Compatibility

Unknown compatibility is represented by a row in `compatibility.csv` with
`status` set to `unknown`. A missing compatibility row also means unknown. Use a
row when the source system explicitly records "not yet assessed" or when a data
quality report should point to the source record.

## Required Tables

### Workers

File: `workers.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `worker_id` | string | required | Maps to `Worker.id`. |
| `name` | string | required | Display only; not identity. |
| `gender` | enum | optional | Allowed: `female`, `male`, `non_binary`, `other`, `prefer_not_to_say`, `unknown`. Imported for review/context, not used for scoring by itself. |
| `home_label` | string | optional | Maps to `Location.label`; use an empty string if postcode/coordinates identify the location. |
| `home_postcode` | string | optional | Synthetic fixtures must not use real home postcodes. |
| `home_latitude` | decimal | optional | Must be paired with longitude when present. |
| `home_longitude` | decimal | optional | Must be paired with latitude when present. |
| `home_area` | string | optional | Area or postcode sector used by scoring. |
| `travel_modes` | list enum | required | Allowed: `drive`, `bus`, `tram`, `train`, `public_transport`, `cycle`, `walk`, `taxi`. |
| `has_car` | boolean | required | Does not imply `drive` unless `travel_modes` also contains `drive`. |
| `skills` | list string | optional | Maps to `Worker.skills`. |
| `languages` | list string | optional | Maps to `Worker.languages`. |
| `willing_areas` | list string | optional | Maps to `Worker.willing_areas`. |
| `current_hours` | decimal | optional | Empty maps to `0.0`. |
| `max_hours` | decimal | optional | Empty maps to `None`. |
| `preference_tags` | list string | optional | Maps to `Worker.preference_tags`. |
| `reliability_score` | decimal 0..1 | optional | Empty maps to `None`; future scoring should audit this carefully. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Service Users

File: `service_users.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `service_user_id` | string | required | Maps to `ServiceUser.id`. |
| `name` | string | required | Display only; not identity. |
| `gender` | enum | optional | Allowed: `female`, `male`, `non_binary`, `other`, `prefer_not_to_say`, `unknown`. Imported for review/context, not used for scoring by itself. |
| `home_label` | string | optional | Maps to `Location.label`. |
| `home_postcode` | string | optional | Synthetic fixtures must not use real home postcodes. |
| `home_latitude` | decimal | optional | Must be paired with longitude when present. |
| `home_longitude` | decimal | optional | Must be paired with latitude when present. |
| `home_area` | string | optional | Area or postcode sector used by scoring. |
| `support_level` | string | required | Tenant-defined normalized support level such as `standard`, `1_to_1`, or `2_to_1`. |
| `required_worker_count` | integer >= 1 | required | Default source value for sessions when a session does not override it. |
| `requires_driver` | boolean | required | Default source value for sessions when a session does not override it. |
| `required_skills` | list string | optional | Maps to `ServiceUser.required_skills`. |
| `communication_needs` | list string | optional | Maps to `ServiceUser.communication_needs`. |
| `interests` | list string | optional | Maps to `ServiceUser.interests`. |
| `risk_tags` | list string | optional | Maps to `ServiceUser.risk_tags`. |
| `preferred_worker_ids` | list worker ID | optional | Unknown worker IDs are warnings unless tenant policy requires errors. |
| `avoided_worker_ids` | list worker ID | optional | Unknown worker IDs are warnings unless tenant policy requires errors. |
| `profile_is_stale` | boolean | required | `true` should emit a data-quality warning. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Sessions

File: `sessions.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `session_id` | string | required | Maps to `Session.id`. |
| `service_user_id` | service-user ID | required | Must exist in `service_users.csv`. |
| `start` | offset ISO 8601 date-time | required | Session start. |
| `end` | offset ISO 8601 date-time | required | Must be after `start`. |
| `location_label` | string | optional | Maps to `Session.location.label`. |
| `location_postcode` | string | optional | Synthetic fixtures must not use real home postcodes. |
| `location_latitude` | decimal | optional | Must be paired with longitude when present. |
| `location_longitude` | decimal | optional | Must be paired with latitude when present. |
| `location_area` | string | optional | Area or postcode sector used by scoring. |
| `required_worker_count` | integer >= 1 | required | Maps to `Session.required_worker_count`. |
| `assigned_worker_ids` | list worker ID | optional | Existing assignments in source state. |
| `assigned_roles` | list string | optional | Must be the same length/order as `assigned_worker_ids`; empty roles can be derived by importer policy. |
| `requires_driver` | boolean | required | Session-specific driver requirement. |
| `priority` | decimal > 0 | optional | Empty maps to `1.0`. |
| `status` | enum | required | Allowed: `unallocated`, `partially_allocated`, `allocated`, `confirmed`, `cancelled`, `rejected`. |
| `travel_buffer_before_minutes` | integer >= 0 | optional | Empty maps to tenant default. |
| `travel_buffer_after_minutes` | integer >= 0 | optional | Empty maps to tenant default. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Availability

File: `availability.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `availability_id` | string | required | Stable row ID for validation and audit. |
| `worker_id` | worker ID | required | Must exist in `workers.csv`. |
| `start` | offset ISO 8601 date-time | required | Availability window start. |
| `end` | offset ISO 8601 date-time | required | Must be after `start`. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

Availability means bookable session time. Travel buffers do not shrink this
window; route feasibility is validated separately.

### Unavailability

File: `unavailability.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `unavailability_id` | string | required | Stable row ID for validation and audit. |
| `worker_id` | worker ID | required | Must exist in `workers.csv`. |
| `start` | offset ISO 8601 date-time | required | Unavailability window start. |
| `end` | offset ISO 8601 date-time | required | Must be after `start`. |
| `reason` | string | optional | Normalized non-sensitive reason label. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Compatibility

File: `compatibility.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `worker_id` | worker ID | required | Must exist in `workers.csv`. |
| `service_user_id` | service-user ID | required | Must exist in `service_users.csv`. |
| `status` | enum | required | Allowed: `appropriate`, `inappropriate`, `unknown`. |
| `reason` | string | optional | Keep free text non-sensitive in committed fixtures. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Pair History

File: `pair_history.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `worker_id` | worker ID | required | Must exist in `workers.csv`. |
| `service_user_id` | service-user ID | required | Must exist in `service_users.csv`. |
| `completed_sessions` | integer >= 0 | required | Empty is invalid; use `0`. |
| `positive_feedback_count` | integer >= 0 | required | Empty is invalid; use `0`. |
| `incident_count` | integer >= 0 | required | Empty is invalid; use `0`. |
| `cancellation_count` | integer >= 0 | required | Empty is invalid; use `0`. |
| `last_session_at` | offset ISO 8601 date-time | optional | Empty maps to `None`. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

### Family Requests

File: `family_requests.csv`

| Column | Type / allowed values | Owner | Notes |
| --- | --- | --- | --- |
| `family_request_id` | string | required | Maps to `FamilyRequest.id`. |
| `service_user_id` | service-user ID | required | Must exist in `service_users.csv`. |
| `requested_worker_ids` | list worker ID | optional | Unknown worker IDs are warnings unless tenant policy requires errors. |
| `avoided_worker_ids` | list worker ID | optional | Unknown worker IDs are warnings unless tenant policy requires errors. |
| `preferred_days` | list integer 0..6 | optional | `0` Monday through `6` Sunday. Order is priority order, most preferred first. |
| `preferred_time_ranges` | list ISO interval | optional | Maps to `FamilyRequest.preferred_time_ranges`. |
| `unavailable_time_ranges` | list ISO interval | optional | Maps to `FamilyRequest.unavailable_windows`. |
| `notes` | string | optional | Do not store unnecessary personal detail in standardized fixtures. |
| `target_sessions_per_week` | integer >= 1 | optional | How many sessions per week this request implies. Empty means there is not enough structure to provision a session from this request. Ignored when `preferred_day_patterns` is present. |
| `target_session_duration_minutes` | integer > 0 | optional | Typical session length implied by the request, shared across every group in `preferred_day_patterns` when present. Required for the request to be provisionable. |
| `preferred_time_window_start` | `HH:MM` local clock time | optional | Earliest acceptable session start time of day. Empty with an end time present means "no earlier than end of day minus duration". |
| `preferred_time_window_end` | `HH:MM` local clock time | optional | Latest acceptable session end time of day. Empty with a start time present means "no later than start of day plus duration". |
| `preferred_day_patterns` | list of `day,day,...:count` groups | optional | Richer alternative to `preferred_days`/`target_sessions_per_week` for requests needing independent day-category groups. Groups are pipe-separated; within a group, days are comma-separated in priority order (most preferred first) followed by `:` and how many sessions/week come from that group. Example: `0,1,3,4:1\|5,6:1` means 1 session/week from {Mon,Tue,Thu,Fri} (in that priority order) plus 1 session/week from {Sat,Sun} -- "1 midweek + 1 weekend session/week", which a single flat `preferred_days` list cannot express (it can only rank one combined list, not enforce one pick from each of two groups). When present, a planner should use this instead of the flat `preferred_days`/`target_sessions_per_week` pair. |
| `source_ref` | string | optional | Human-readable source row/object reference. |

`target_sessions_per_week`, `target_session_duration_minutes`,
`preferred_time_window_*`, and `preferred_day_patterns` exist so a planner can
provision a concrete session from a request that has no matching session yet,
instead of the request sitting as inert preference data. Every group in
`preferred_day_patterns` shares the same `target_session_duration_minutes` and
time window -- mixed-duration patterns (for example "1x6hr plus 2x3hr
sessions") and term-time-vs-holiday variants are still not represented here
and should be flagged for human review via `notes` instead of guessed.

### Import Manifest

File: `import_manifest.json`

Required fields:

- `import_run_id`: stable ID for this import attempt.
- `imported_at`: offset ISO 8601 date-time.
- `source_system`: `standardized-csv` for direct CSV imports, or a tenant
  adapter name.
- `source_refs`: array of source file/object references.

Optional fields:

- `generated_state_snapshot_id`: empty or absent until persistence exists.
- `notes`: non-sensitive operator/developer notes.

The importer derives `row_counts`, `validation_warnings`, and per-row
`SourceReference` values from the manifest, file name, `source_ref`, and row
number.

## Data Quality Flags

Import adapters must emit warnings for:

- Missing or duplicate IDs.
- Ambiguous name matches.
- Missing postcode.
- Missing travel mode.
- Missing availability.
- Stale service-user profile.
- Date request parsed with low confidence.
- Worker appears in source data but not current staff list.
- Service user appears in booking source but not profile/postcode source.
