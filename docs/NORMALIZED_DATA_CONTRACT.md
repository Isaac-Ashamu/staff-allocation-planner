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

## Required Tables

### Workers

- `id`: stable tenant-scoped worker ID.
- `name`
- `home_location`
- `travel_modes`
- `has_car`
- `skills`
- `languages`
- `willing_areas`
- `availability`
- `unavailability`
- `current_hours`
- `max_hours`
- `preference_tags`

### Service Users

- `id`: stable tenant-scoped service-user ID.
- `name`
- `home_location`
- `support_level`
- `required_worker_count`
- `requires_driver`
- `required_skills`
- `communication_needs`
- `interests`
- `risk_tags`
- `preferred_worker_ids`
- `avoided_worker_ids`
- `profile_is_stale`

### Sessions

- `id`
- `service_user_id`
- `start`
- `end`
- `location`
- `required_worker_count`
- `assigned`
- `requires_driver`
- `priority`
- `status`
- `travel_buffer_before_minutes`
- `travel_buffer_after_minutes`

### Compatibility

- `worker_id`
- `service_user_id`
- `status`: `appropriate`, `inappropriate`, or `unknown`.
- `reason`

### Pair History

- `worker_id`
- `service_user_id`
- `completed_sessions`
- `positive_feedback_count`
- `incident_count`
- `cancellation_count`
- `last_session_at`

### Family Requests

- `id`
- `service_user_id`
- `requested_worker_ids`
- `avoided_worker_ids`
- `preferred_days`
- `preferred_time_ranges`
- `notes`
- `source_ref`

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
