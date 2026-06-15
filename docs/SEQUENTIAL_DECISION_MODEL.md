# Sequential Decision Model

This model treats bookings as a sequential planning problem. The system observes
the current operating state, recommends one or more allocation actions, receives
new information, updates the state, and repeats.

## Sets and Indices

- `t`: decision epoch. A booking team may make decisions hourly, daily, or when
  new requests arrive.
- `i in I`: service users.
- `w in W`: support workers.
- `s in S_t`: sessions known at time `t`.
- `r in R_t`: family/date/ad hoc requests known at time `t`.
- `m in M`: travel modes, for example `drive`, `bus`, `tram`, `train`, `cycle`,
  `walk`, `taxi`.
- `k in K`: skill, risk, preference, or compatibility attributes.
- `tenant`: company using the SaaS product.

## State

The state `X_t` is everything known immediately before making decisions at time
`t`.

### Service User State

- `su.id`: stable service-user identifier.
- `su.name`
- `su.location`: home postcode/geocode or usual pickup point.
- `su.support_level`: for example `1:1`, `2:1`, group support.
- `su.requires_driver`: whether a driver/car is required.
- `su.risk_profile`: structured risks such as absconding, road safety, physical
  distress, medical risk, venue restrictions.
- `su.communication_needs`: PECS, short instructions, processing time, visual
  supports, language needs.
- `su.interests`: activities and venues likely to work.
- `su.dislikes_or_triggers`
- `su.family_preferences`: preferred days, times, workers, avoided workers,
  travel constraints, activity constraints.
- `su.consent_and_policy_flags`: photo consent, peer matching consent, profile
  freshness, internal review requirements.

### Worker State

- `worker.id`
- `worker.name`
- `worker.home_location`
- `worker.temporary_locations_t`: known college/work/placement locations by
  date/time.
- `worker.travel_modes`
- `worker.has_car`
- `worker.languages`
- `worker.skills`
- `worker.experience_tags`
- `worker.allowed_support_levels`: for example whether suitable for high-risk
  `2:1`.
- `worker.availability_calendar_t`
- `worker.unavailability_calendar_t`
- `worker.current_allocations_t`
- `worker.max_hours_policy`
- `worker.current_hours_t`
- `worker.reliability_features_t`: cancellation and cover history, used with
  care and explainability.
- `worker.preference_notes`: areas avoided, preferred service users, preferred
  hours.

### Session State

- `session.id`
- `session.service_user_id`
- `session.date`
- `session.start_time`
- `session.end_time`
- `session.duration_minutes`
- `session.location`
- `session.required_worker_count`
- `session.assigned_workers_t`
- `session.required_travel_mode`: optional, for example `driver`.
- `session.flexibility_window`: allowed alternative starts/dates.
- `session.priority`: regular committed session, ad hoc, urgent cover, holiday
  extra, family requested, statutory/contractual priority.
- `session.status`: unallocated, partially allocated, allocated, confirmed,
  cancelled, rejected.

### Compatibility State

- `compatibility(worker, su)`: explicit appropriate/inappropriate/unknown.
- `pair_history(worker, su)`: previous sessions, outcomes, family feedback,
  incidents, cancellations, continuity strength.
- `risk_requirements(su)`: required skills or experience thresholds.

### Travel State

- `travel_time_t(worker, origin, destination, mode, departure_time)`
- `travel_cost_t(worker, origin, destination, mode, departure_time)`
- `travel_reliability_t(mode, route, time)`: confidence/buffer factor.
- `geo_distance(origin, destination)`
- `postcode_area(origin)`, `postcode_area(destination)`

### System State

- `tenant.rules`: configurable policies for the company.
- `audit_log_t`: prior recommendations, accepted decisions, rejected decisions,
  override reasons.
- `data_quality_t`: missing availability, missing postcode, stale profile,
  ambiguous name match, unparsed request.

## Actions

An action `a_t` is a proposed change to the booking plan.

Atomic actions:

- `assign(worker=w, session=s, role=lead|second)`
- `unassign(worker=w, session=s)`
- `swap(worker_a, worker_b, session_a, session_b)`
- `move_session(session=s, new_start, new_end, new_location?)`
- `split_session(session=s, segments=[...])`
- `request_more_information(request_id)`
- `flag_for_human_review(session=s, reason)`
- `reject_candidate(worker=w, session=s, reason)`
- `confirm_allocation(session=s)`
- `create_cover_task(session=s)`

Composite actions:

- `allocate_session`: assign all required workers to one session.
- `allocate_day`: produce a plan for all unfilled sessions on a day.
- `repair_plan`: resolve conflicts after illness, cancellation, new date
  request, or family change.

## Decisions

A decision `d_t` is the selected action or set of actions from the feasible
action set `A(X_t)`.

Examples:

- Choose the top-ranked worker for a second-worker gap.
- Choose whether to prioritize continuity or lower travel burden.
- Choose whether to leave a session unallocated because all candidates fail
  hard constraints.
- Choose whether to offer alternative times to the family.
- Choose whether to ask a worker if they can flex availability.

The system should recommend `d_t`, but a human should approve externally visible
changes unless the tenant config explicitly allows automation.

## Exogenous Information

Exogenous information `W_{t+1}` arrives after the decision and is not controlled
by the system.

Examples:

- New family date request.
- Family changes preferred day/time.
- Worker updates availability.
- Worker calls in sick.
- Worker loses car access or reports transport issue.
- Public transport disruption.
- Traffic delay.
- Session feedback or incident report.
- New profile/risk information.
- New staff member starts.
- Staff member leaves.
- Booking system import changes.
- Service user moves address.
- School/college holiday changes.

## Transition Function

The transition function updates the state:

```text
X_{t+1} = S_M(X_t, d_t, W_{t+1})
```

Examples:

- If `assign(w, s)` is approved, then `w` is added to
  `session.assigned_workers`, worker availability is reduced, and audit log is
  updated.
- If a worker cancellation arrives, affected sessions return to unallocated or
  partially allocated state.
- If travel disruption arrives, travel feasibility and candidate rankings are
  recomputed.
- If an incident report arrives, pair history and risk requirements may change.

## Constraints

Hard constraints:

- Worker must be available for the full session plus travel buffers.
- Worker must be able to reach the session after previous commitments.
- Worker must satisfy driver/car requirement where required.
- Worker must not be explicitly inappropriate for the service user.
- Worker must satisfy required skills/training/risk thresholds.
- Session must have required worker count.
- Worker must not exceed legal/company working-hour rules.
- Safeguarding, consent, and policy blocks must be respected.

Soft constraints:

- Prefer family-requested workers.
- Prefer continuity where previous sessions went well.
- Prefer lower travel time and lower travel uncertainty.
- Prefer suitable communication/language match.
- Prefer activity/interest fit.
- Balance worker load fairly.
- Avoid avoidable back-to-back high-risk sessions.
- Prefer plans that leave enough future capacity for known requests.

## Objective Function

The system should maximize expected plan value over time:

```text
max E[ sum_t gamma^t * C(X_t, d_t) ]
```

Where `C(X_t, d_t)` combines:

- `coverage_value`: sessions allocated and confirmed.
- `safety_value`: risk-appropriate allocation.
- `preference_value`: family and worker preferences satisfied.
- `continuity_value`: known successful relationships preserved.
- `travel_value`: lower travel time, cost, and disruption risk.
- `fairness_value`: balanced hours and opportunities.
- `robustness_value`: plan remains workable when likely changes occur.
- `admin_value`: fewer manual follow-ups and fewer rejected allocations.
- `penalty`: conflicts, missing data, policy warnings, stale profile, high
  uncertainty.

For SaaS use, weights should be tenant-configurable and explainable.

## Candidate Recommendation Score

For a candidate action `assign(w, s)`, a practical first score is:

```text
score(w, s) =
  hard_feasible(w, s) *
  (
    beta_1 * explicit_compatibility(w, su)
  + beta_2 * family_requested(w, su, s)
  + beta_3 * continuity(w, su)
  + beta_4 * travel_fit(w, s)
  + beta_5 * skill_risk_fit(w, su)
  + beta_6 * preference_fit(w, s)
  + beta_7 * load_balance(w)
  + beta_8 * reliability_fit(w, s)
  - beta_9 * uncertainty_penalty(w, s)
  )
```

Where:

- `hard_feasible(w, s)` is `0` if any hard constraint fails, otherwise `1`.
- `explicit_compatibility` comes from the matching/appropriateness table.
- `family_requested` comes from date requests, ad hocs, emails, and notes.
- `continuity` comes from previous allocated/completed sessions.
- `travel_fit` uses mode-specific travel time and buffer feasibility.
- `skill_risk_fit` compares worker skills to service-user risks.
- `preference_fit` compares worker and family preferences.
- `load_balance` discourages overloading the same workers.
- `reliability_fit` should be carefully designed and audited to avoid unfair
  treatment of protected/health-related absence.
- `uncertainty_penalty` increases when key data is missing or stale.

## Observability and Audit

Every recommendation must store:

- Input state snapshot ID.
- Candidate action.
- Hard constraints passed/failed.
- Score components.
- Data quality warnings.
- Human decision.
- Override reason if chosen candidate was not top-ranked.
- Outcome after completion: completed, cancelled, incident, positive feedback,
  family comment.

This audit trail is essential for safeguarding, debugging, and SaaS trust.
