# json-drift

[![skills.sh](https://skills.sh/b/mahabubul1/json-drift)](https://skills.sh/mahabubul1/json-drift)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

An agent skill that catches API response drift before your users do.

A backend change turns `"price": 120.5` into `"price": "120.50"`, an empty `settings: {}` into `[]`, or `shipped_at` into `shippedAt`, and a mobile app crashes on a screen nobody tested. `json-drift` compares what an API returns with what its clients expect, then reports what changed, why it probably happened, and how to fix it without breaking older app versions.

Works with Claude Code, Claude.ai, and any agent that supports the [Agent Skills](https://agentskills.io/) format.

## Installation

```bash
npx skills add mahabubul1/json-drift
```

Skills install to the current project by default. Add `-g` to make it available across projects.

To install manually, copy this folder into your agent's skills directory, for example `~/.claude/skills/json-drift`.

## Usage

The skill is available automatically once installed. The agent uses it when a task involves a response that no longer matches what its clients expect.

**Use when:**

- An app or frontend suddenly broke after a backend change
- A Flutter/Dart or TypeScript client throws a parse or type error on an API response
- You have two API responses and want to know what changed
- You want to check an endpoint against its contract or Swagger docs
- You want contract tests or a CI check for API responses

**Examples:**

```
The Flutter profile screen crashes since yesterday's deploy. Here's the response from /api/me.
```

```
Compare this response against our openapi.yaml for GET /orders/{id}.
```

```
Set up a CI check so our API responses can't drift from the contract again.
```

## What it detects

| Severity | Findings |
|---|---|
| **Breaking** | Removed field, renamed field, key casing change (`created_at` to `createdAt`), type change, unexpected `null`, number sent as string, `[]` instead of `{}`, `0/1` instead of boolean, object flattened to a string, field missing in some array items |
| **Warning** | Date or string format changed, new enum value, field not allowed by `additionalProperties: false`, integer field now has decimals |
| **Info** | New fields, paths that could not be verified (arrays empty in the response, null-only baselines) |

Each breaking finding comes with a likely cause and fix, covering Laravel/PHP in depth plus Node, Django, and Rails.

## Example report

Run the checker against the bundled examples:

```bash
python scripts/json_drift.py compare \
  --baseline examples/baseline-snapshot.json \
  --actual examples/actual-drifted.json
```

An excerpt of the report:

| Path | Change | Expected | Actual | Likely fix |
|---|---|---|---|---|
| `data.balance` | Number is now serialized as a string | number | string (e.g. "1250.50") | Laravel: cast it ('balance' => 'float') in $casts or the API Resource; 'decimal:N' casts and some DB drivers produce strings. |
| `data.is_active` | Boolean is now sent as 0/1 | boolean | integer (e.g. 1) | MySQL tinyint without a cast. Laravel: add 'is_active' => 'boolean' to $casts. |
| `data.orders[].shipped_at` | Key casing changed: `shipped_at` → `shippedAt` | shipped_at | shippedAt | Usually a camelCase/snake_case switch in a Resource or serializer. Keep the old key or update every client. |

## Supported baselines

| Baseline | Details |
|---|---|
| **Snapshot** | A saved copy of a known-good response |
| **JSON Schema** | Draft-07 to 2020-12, including `$ref`, `allOf`/`oneOf`/`anyOf`, `nullable`, `enum`, and `format` |
| **OpenAPI 3 / Swagger 2** | Select the path, method, and status to compare against |
| **Client models** | Dart (freezed, json_serializable), TypeScript, and zod. The agent converts them to a schema first |

## Command-line usage

`scripts/json_drift.py` is a single file that runs on Python 3.8+ with no required dependencies. PyYAML is needed only for `.yaml` and `.yml` files.

```bash
# Compare against a saved snapshot
python scripts/json_drift.py compare --baseline old.json --actual new.json

# Compare a live response against an OpenAPI spec
curl -s "$API/api/users/1" -H "Authorization: Bearer $TOKEN" \
  | python scripts/json_drift.py compare --baseline openapi.json --openapi-path "/api/users/{id}" -

# Build a schema contract from known-good responses
python scripts/json_drift.py infer good1.json good2.json --out contracts/users.show.schema.json
```

### `compare` options

| Option | Description | Default |
|---|---|---|
| `--baseline FILE` | Snapshot, JSON Schema, or OpenAPI spec (required) | |
| `--actual FILE...` | Response files to check, or `-` for stdin | |
| `--baseline-type TYPE` | `auto`, `snapshot`, `schema`, or `openapi` | `auto` |
| `--openapi-path PATH` | OpenAPI path template, for example `/users/{id}` | |
| `--method METHOD` | HTTP method to look up in the spec | `get` |
| `--status CODE` | Response status to look up in the spec | `200` |
| `--unwrap PATH` | Dot path to compare inside the response, for example `data` | |
| `--ignore GLOB` | Paths to skip, for example `'meta.**'` for everything under `meta`. Repeatable | |
| `--format FORMAT` | `md` or `json` | `md` |
| `--fail-on LEVEL` | `breaking`, `warning`, `info`, or `never` | `breaking` |
| `--out FILE` | Write the report to a file instead of stdout | |

`infer` takes one or more sample files, plus `--out FILE` and `--title TITLE`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No drift at or above the `--fail-on` level |
| `1` | Drift found at or above the `--fail-on` level |
| `2` | Usage error, missing file, or invalid JSON |

## CI integration

[`references/ci-integration.md`](references/ci-integration.md) covers a GitHub Actions workflow, a Laravel/PHPUnit contract test, a script for snapshotting several endpoints, and how to update a contract on purpose.

## Skill structure

```
SKILL.md                  Instructions the agent follows
scripts/json_drift.py     The drift checker
references/
  backend-pitfalls.md     Cause and fix per finding (Laravel, Node, Django, Rails, Dart/TS hardening)
  model-to-schema.md      Dart, TypeScript, and zod models to JSON Schema
  ci-integration.md       GitHub Actions, PHPUnit contract test, multi-endpoint script
examples/                 Sample snapshot, schema, OpenAPI spec, and drifted responses
tests/                    Unit tests
```

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE) © BlackDevs
