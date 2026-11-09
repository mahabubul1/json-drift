# Keeping contracts checked

## Contents
1. Layout and exit codes
2. Snapshot several endpoints at once
3. GitHub Actions
4. Laravel / PHPUnit contract test
5. Updating a contract on purpose

---

## 1. Layout and exit codes

```
contracts/
  users.show.schema.json        # GET /api/users/{id}
  orders.index.schema.json      # GET /api/orders
  endpoints.txt                 # list used by the snapshot script
```

Generate each contract from known-good responses with `json_drift.py infer`, then **review it**: infer
can't know which fields are optional or which enum values exist. Remove keys from `required` that
are legitimately optional, add `enum` lists, and loosen `format` where needed. Commit schemas, not raw
responses — raw responses contain real user data.

`compare` exits `0` (clean), `1` (drift at/above `--fail-on`, default `breaking`), `2` (bad input).
Use `--fail-on warning` for strict pipelines, `--fail-on never` for report-only jobs.

## 2. Snapshot several endpoints at once

`endpoints.txt` — one `name METHOD path` per line:

```
users.show GET /api/users/1
orders.index GET /api/orders?per_page=5
```

`scripts/check-contracts.sh`:

```bash
#!/usr/bin/env bash
# Usage: API_URL=https://staging.example.com API_TOKEN=... ./scripts/check-contracts.sh
set -euo pipefail
mkdir -p .drift
status=0
while read -r name method path; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  curl -sS -X "$method" "$API_URL$path" -H "Accept: application/json" \
       -H "Authorization: Bearer $API_TOKEN" -o ".drift/$name.json"
  if ! python json_drift.py compare --baseline "contracts/$name.schema.json" \
        --actual ".drift/$name.json" --out ".drift/$name.md"; then
    echo "❌ $name drifted"; status=1
  else
    echo "✅ $name"
  fi
done < contracts/endpoints.txt
cat .drift/*.md > .drift/report.md
exit $status
```

Point `json_drift.py` at wherever the script lives in the project (copy it into `scripts/` or `tools/`).

## 3. GitHub Actions

```yaml
name: API contract
on:
  pull_request:
  schedule: [{ cron: "0 3 * * *" }]   # nightly against staging catches drift from other teams
jobs:
  contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - name: Check API responses against contracts
        env:
          API_URL: ${{ vars.STAGING_API_URL }}
          API_TOKEN: ${{ secrets.STAGING_API_TOKEN }}
        run: ./scripts/check-contracts.sh
      - name: Add report to job summary
        if: always()
        run: cat .drift/report.md >> "$GITHUB_STEP_SUMMARY" || true
```

For PR checks without a live server, generate the actual responses inside the test suite instead (next section).

## 4. Laravel / PHPUnit contract test

Runs in the normal test suite against the test database — no deployed server needed.

```php
<?php
namespace Tests\Feature\Contracts;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

class UserContractTest extends TestCase
{
    use RefreshDatabase;

    public function test_users_show_matches_contract(): void
    {
        $user = User::factory()->hasOrders(2)->create();   // non-empty arrays = full coverage
        $response = $this->actingAs($user)->getJson("/api/users/{$user->id}")->assertOk();

        $this->assertMatchesContract('users.show', $response->getContent());
    }

    private function assertMatchesContract(string $name, string $json): void
    {
        $actual = tempnam(sys_get_temp_dir(), 'drift');
        file_put_contents($actual, $json);
        $cmd = sprintf('python3 %s compare --baseline %s --actual %s 2>&1',
            escapeshellarg(base_path('tools/json_drift.py')),
            escapeshellarg(base_path("contracts/$name.schema.json")),
            escapeshellarg($actual));
        exec($cmd, $output, $code);
        unlink($actual);
        $this->assertSame(0, $code, "API contract drift in $name:\n" . implode("\n", $output));
    }
}
```

Factories should populate optional relations and fields so the check covers them.
For a pure-PHP alternative without Python, `opis/json-schema` validates against the same schema files
(but without drift-specific hints).

## 5. Updating a contract on purpose

When a change is intentional: re-run `infer` on the new good responses, diff the old and new schema in
the PR (`json_drift.py compare --baseline old.schema.json --actual new-response.json` shows what
clients will feel), and bump the API version if anything is breaking.
