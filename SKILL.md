---
name: json-drift
description: Detect API response drift — when a backend's JSON response no longer matches what clients expect (removed or renamed fields, type changes, numbers sent as strings, unexpected nulls, [] instead of {}, 0/1 instead of booleans, changed date formats, new enum values). Compares a live or sample response against a saved snapshot, JSON Schema, OpenAPI spec, or a frontend model, and returns a severity-ranked report with fixes. Use this whenever a user says an app/frontend "suddenly broke" after a backend change, a Flutter/Dart or TypeScript client throws a parse/type error on an API response, they paste two API responses and ask what changed, they want to check an endpoint against its contract or Swagger docs, or they want contract/regression tests or a CI check for API responses — even if they never say "drift".
---

# json-drift

Find out exactly how an API's JSON response has drifted from what its clients expect, why it
happened, and how to fix it without breaking older app versions.

Most "the app crashed after the deploy" bugs are not logic bugs. They are small contract changes:
`"price": 120.5` became `"price": "120.50"`, `settings: {}` became `[]`, `is_active` became `1`,
`created_at` switched format, a key was renamed to camelCase. Typed clients (Dart, Kotlin, Swift,
TypeScript with runtime validation) fail hard on these. This skill makes the diff mechanical and
the diagnosis fast.

## The tool

`scripts/json_drift.py` — Python 3.8+, no dependencies. Run it instead of eyeballing JSON: it
walks every path, merges multiple samples, and knows the common serializer pitfalls.

```bash
# Compare against a saved good response (snapshot)
python scripts/json_drift.py compare --baseline contracts/users.show.json --actual response.json

# Against a JSON Schema contract
python scripts/json_drift.py compare --baseline contracts/users.show.schema.json --actual response.json

# Against an OpenAPI/Swagger spec (JSON; YAML works if PyYAML is installed)
python scripts/json_drift.py compare --baseline openapi.json \
  --openapi-path "/api/users/{id}" --method get --status 200 --actual response.json

# Pipe straight from curl; several actual files can be given (they're merged)
curl -s "$API/users/1" -H "Authorization: Bearer $TOKEN" | python scripts/json_drift.py compare --baseline base.json -

# Make a contract from known-good responses
python scripts/json_drift.py infer good1.json good2.json --title "GET /api/users/{id}" --out contracts/users.show.schema.json
```

Useful flags: `--ignore 'meta.**'` / `--ignore 'data[].updated_at'` (repeatable path globs),
`--unwrap data` (compare only the inner payload of the actual response), `--format json`,
`--out report.md`, `--fail-on breaking|warning|info|never` (exit 1 when drift at that level exists).

Paths look like `data.orders[].total`: `[]` means "every item of the array".

## Workflow

### 1. Pin down the two sides

You need a **baseline** (what clients expect) and an **actual** response (what the API returns now).
Work out which baseline exists, in this order of trust:

1. A JSON Schema / OpenAPI spec in the repo (`openapi.*`, `swagger.*`, `docs/api*`, `contracts/`, `*.schema.json`).
2. A saved good response: fixtures, Postman examples, test snapshots, an old response the user pastes.
3. The client's model classes (Dart `fromJson`, freezed / json_serializable, TS interfaces, zod schemas).
   This is often the *real* contract, because it's what crashes. Convert it to a JSON Schema first —
   see `references/model-to-schema.md` — then compare against that.

If the user only has a vague complaint ("Flutter app broke on the profile screen"), find the endpoint
that screen calls and the model that parses it before asking them anything.

For the actual side: use what they pasted, run the endpoint (curl / HTTPie / a Laravel feature test /
`php artisan tinker`), or read the API Resource / serializer code if nothing can be executed. When
calling a live API, put tokens in env vars and never write them into files or reports. Prefer
responses that contain non-empty arrays and populated optional fields: an empty `orders: []` can't
reveal drift inside order items (the report marks such paths as *unverified*).

Save both sides as files (e.g. `/tmp/drift/baseline.json`, `/tmp/drift/actual.json`) so the run is
reproducible and the user can re-run it.

### 2. Run the comparison

Run `compare`. Use several actual samples when you have them (a list endpoint plus a detail endpoint,
a user with and without optional data) — the tool merges them, which is how it spots
"sometimes missing" keys and variable types.

Snapshot vs schema matters:
- A **snapshot** says "this is what one good response looked like". Every key in it is treated as
  required; the tool can't know enums, and a field that was `null` in the snapshot can't be type-checked.
- A **schema/spec** knows which fields are optional, nullable, and which enum values exist, so it gives
  fewer false alarms. If the user will be checking this endpoint again, offer to `infer` a schema from
  good responses and commit it — schemas hold no real user data, unlike raw snapshots.

Add `--ignore` for values that legitimately vary in shape (debug blocks, `meta.**`), not to hide real findings.

### 3. Diagnose each finding

The report ranks findings:
- **breaking** — will crash or silently corrupt a typed client (removed/renamed field, type change,
  unexpected null, number-as-string, `[]` for `{}`, 0/1 for booleans, key casing change).
- **warning** — likely bugs (date/string format changed, new enum value, field not allowed by
  `additionalProperties: false`, an integer field that now has decimals in snapshot mode).
- **info** — new fields (additive and usually safe) and paths that couldn't be verified.

For every breaking finding and warning, find the cause in code rather than guessing: grep the backend
for the field name and check the serializer layer (API Resources, transformers, `$casts`, `$hidden`,
`$appends`, `whenLoaded`, query `select()`s, recent migrations), and check `git log -p` on those files
if the repo is available. `references/backend-pitfalls.md` maps each finding kind to its usual
causes and fixes for Laravel/PHP, plus Node, Django/DRF, and Rails notes. Read it when a finding's
cause isn't obvious.

Watch for false positives before reporting:
- A "rename" is a similarity heuristic. Confirm it in code or by value (same value under both keys).
- A snapshot taken from one record may show `12` for a field that is really decimal; check the column type.
- Different records legitimately differ (an admin user has fields a customer doesn't). Compare like with like.

### 4. Decide where to fix it

Mobile apps can't be force-updated, so old app versions keep calling the API for months. Default to
fixing on the **API side** in a backward-compatible way:
- Restore the type with a cast or explicit Resource formatting.
- For renames, return **both** keys for at least one release and mark the old one deprecated.
- For new nullability, return a sensible default or keep the key non-null, unless null is truly
  meaningful — then make the client nullable *and* keep the API safe for old clients.
- Intentional breaking changes belong in a new API version (`/v2`) or behind a client-version check.

Fix the client instead when the API's new shape is the correct one and every client ships together
(web-only frontends, internal tools). For Dart, defensive parsing helpers are in
`references/backend-pitfalls.md` ("Hardening the client").

### 5. Report back

Lead with the verdict, then a table — the tool's markdown output is a good base, but add what you
learned from the code:

```markdown
## API drift: GET /api/users/{id} — 4 breaking, 1 warning

| Path | What changed | Cause | Fix |
|---|---|---|---|
| data.balance | number → string "1250.50" | `'balance' => 'decimal:2'` cast (User.php:41) | cast to `float` in UserResource |
| data.settings | {} → [] when empty | `json_decode(..., true)` of empty object | return `(object) $settings` |
```

Then: concrete patches (show diffs or edit the files if the user wants), which client
screens/models are affected, and one line on prevention.

### 6. Prevent the next one (offer, don't impose)

If the project has no contract checks, offer to set one up — `references/ci-integration.md` has
ready-to-use pieces: a GitHub Actions workflow, a Laravel feature test that compares a response with a
committed schema, and a script that snapshots several endpoints at once. Keep contracts in
`contracts/` (or the project's existing convention) named after the endpoint.

## Reference files

- `references/backend-pitfalls.md` — cause → fix for every finding kind; Laravel/PHP in depth, Node, Django, Rails; Dart/TS client hardening.
- `references/model-to-schema.md` — turn Dart/freezed/json_serializable, TypeScript and zod models into a JSON Schema baseline.
- `references/ci-integration.md` — GitHub Actions, Laravel/PHPUnit contract test, multi-endpoint snapshot script, exit codes.
