# Normalized Data Contract

Import adapters should convert each tenant's source files into this contract
before scoring. This keeps the recommender company-agnostic.

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

## provider Source Mapping

The current local data maps approximately as follows.

- `Import sheet for kids <month>.xlsx/Bookings` -> `Sessions`
- `Import sheet for kids <month>.xlsx/Matching` -> `Compatibility`
- `Staff and SU names and postcode.xlsx/STAFF` -> `Workers`
- `Staff and SU names and postcode.xlsx/SU` -> `Service Users`
- `July 2026 Ad hocs.xlsx` -> `Family Requests`
- Date request `.msg` files -> `Family Requests`
- `data\availability report\*.pdf` -> `Worker.availability` and
  `Worker.unavailability`
- Feedback CSV -> `Pair History`, interests, profile-staleness warnings
- Incident CSV -> `Pair History`, risk warnings
- Profile JSON -> `Service Users`
- Illness/cover workbook -> reliability and exogenous event history

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
