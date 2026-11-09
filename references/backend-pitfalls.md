# Backend pitfalls: finding kind → cause → fix

## Contents
1. Finding kinds at a glance
2. Laravel / PHP (most common sources of drift)
3. Node / Express / Prisma
4. Django / DRF
5. Rails
6. Hardening the client (Dart, TypeScript)

---

## 1. Finding kinds at a glance

| Kind | Typical cause | Backward-compatible fix |
|---|---|---|
| `field_removed` | Column dropped/renamed, `$hidden`, Resource no longer returns it, conditional `when*()`, `select()` without the column | Return the field again (null or default if the data is gone) and deprecate it |
| `field_renamed` / `casing_changed` | Resource rewritten, camelCase middleware/serializer added, column renamed in migration | Send both old and new keys for ≥1 release |
| `sometimes_missing` | `whenLoaded`, `when`, `mergeWhen`, null-stripping middleware, relation eager-loaded on some routes only | Always emit the key; use `null` for "not loaded" |
| `became_nullable` | Nullable column / left join / deleted relation / missing default | Default value or keep non-null; if null is real, version the change |
| `number_as_string` | `decimal:N` casts, DB driver returning strings, `number_format()`, BCMath/money libraries | Cast to `int`/`float` in the serializer; send money as integer minor units if precision matters |
| `int_to_decimal` | Column changed to DECIMAL/FLOAT, computed averages | Keep integer fields integral; change client type to num/double if decimals are intended |
| `bool_as_int` / `bool_as_string` | MySQL `TINYINT(1)` without a cast, raw queries, `DB::select` | Boolean cast |
| `empty_object_as_array` | PHP associative array that happens to be empty encodes as `[]` | `(object) $arr`, `new \stdClass`, `JSON_FORCE_OBJECT`, or `ArrayObject` |
| `object_flattened` | Relation not loaded, so the raw foreign key or a string is returned | Eager-load, or emit `{ "id": .. }`/null consistently |
| `format_changed` | Carbon `serializeDate` override, `$dateFormat`, `date:` casts, timezone config change, `""` for missing dates | One format everywhere: ISO-8601 with offset; null for missing |
| `new_enum_value` | New status added on the backend | Clients need an `unknown` fallback; announce enum additions |
| `unexpected_field` | Field leaked into a strict contract (often internal/debug data) | Remove it or update the contract deliberately |

---

## 2. Laravel / PHP

**Numbers as strings.** `'price' => 'decimal:2'` returns a *string* ("120.50") by design. Older
MySQL PDO setups (or `PDO::ATTR_EMULATE_PREPARES`/`ATTR_STRINGIFY_FETCHES`) return every column
as a string. `DB::select()` and `->toBase()` results skip model casts entirely.

```php
// Model
protected $casts = ['price' => 'float', 'stock' => 'integer', 'is_active' => 'boolean'];
// or explicitly in the Resource (wins regardless of the driver)
'price' => (float) $this->price,
'stock' => (int) $this->stock,
```

**Empty `{}` becomes `[]`.** `json_decode($json, true)` + `json_encode` loses the difference between
an empty object and an empty list. Typical victims: settings, metadata, translations, `errors`.

```php
'settings' => (object) ($this->settings ?? []),     // always {}
// or cast the column: protected $casts = ['settings' => AsArrayObject::class];
```

**Conditional keys.** `$this->whenLoaded('orders')`, `$this->when($cond, ...)`, and `mergeWhen`
*omit the key* when the condition is false. Routes that forget to eager-load produce a different shape.

```php
'orders' => OrderResource::collection($this->whenLoaded('orders')),   // key vanishes
// stable alternative when clients always expect the key:
'orders' => $this->relationLoaded('orders') ? OrderResource::collection($this->orders) : [],
```

**Hidden/appended attributes.** `$hidden`, `makeHidden()`, `$visible` remove keys; `$appends` adds
them on models but not when a Resource builds its own array.

**Dates.** Laravel 7+ serializes as ISO-8601 UTC (`2026-01-10T08:30:00.000000Z`). Overrides that
cause `format_changed`: `serializeDate()` on the model, `'created_at' => 'datetime:d/m/Y'` casts,
manual `->format()` in Resources, `config/app.php` timezone changes. Keep one format, prefer ISO-8601,
and send `null`, not `""`, for missing dates.

**Booleans.** MySQL has no real boolean; without a `boolean` cast you get `0/1`. Raw queries
always need manual casting.

**Pagination wrappers.** `->paginate()` returns `data`, `links`, `meta`; `->simplePaginate()` has a
different `meta`; returning a plain collection drops the wrapper entirely. Switching between these
is a breaking change for list screens — use `--unwrap data` to compare the items separately and
compare wrappers on their own.

**camelCase switches.** Packages or middleware that convert keys (e.g. a "case converter" response
middleware) rename *every* key at once — the report shows a wall of `casing_changed`. Roll back or
send both formats behind a header/version.

**Find the cause quickly**

```bash
grep -rn "'balance'" app/Http/Resources app/Models database/migrations
git log -p --since="2 weeks ago" -- app/Http/Resources app/Models
php artisan route:list --path=users      # which controller/resource serves the endpoint
```

---

## 3. Node / Express / Prisma

- Prisma `Decimal` and `BigInt` don't serialize as JSON numbers: `Decimal` becomes a string, `BigInt`
  throws unless a custom `toJSON` is set. Convert explicitly (`Number(x)` when safe, or string by contract).
- `pg` returns `NUMERIC`/`BIGINT` columns as strings by default (`pg.types.setTypeParser` changes it).
- `undefined` properties are dropped by `JSON.stringify` → `sometimes_missing`. Use `null`.
- Mongoose `toJSON` transforms, virtuals and `_id` vs `id` mapping cause renames.

## 4. Django / DRF

- `DecimalField` serializes as a string unless `COERCE_DECIMAL_TO_STRING = False` (or
  `coerce_to_string=False` per field).
- `SerializerMethodField` returning `None`, or `required=False` fields, produce nulls/missing keys.
- `DATETIME_FORMAT` in `REST_FRAMEWORK` settings changes every date at once.
- camelCase packages (`djangorestframework-camel-case`) rename all keys.

## 5. Rails

- `ActiveRecord` decimals serialize as strings in `as_json` (BigDecimal). Use `.to_f` or a serializer.
- Jbuilder/AMS changes, `except:`/`only:` options, and `ActiveSupport::JSON::Encoding.time_precision`
  changes (dates gain/lose milliseconds).

---

## 6. Hardening the client

Fixing the API is the priority, but clients that tolerate harmless variation break less often.

**Dart / Flutter**

```dart
num? asNum(dynamic v) => v is num ? v : (v is String ? num.tryParse(v) : null);
int? asInt(dynamic v) => v is int ? v : (v is num ? v.toInt() : (v is String ? int.tryParse(v) : null));
bool asBool(dynamic v) => v == true || v == 1 || v == '1' || v == 'true';
Map<String, dynamic> asMap(dynamic v) => v is Map<String, dynamic> ? v : <String, dynamic>{}; // [] → {}
DateTime? asDate(dynamic v) => v is String && v.isNotEmpty ? DateTime.tryParse(v) : null;
```

With json_serializable, attach these via `@JsonKey(fromJson: asNum)`; with enums use
`@JsonKey(unknownEnumValue: Status.unknown)` so new enum values don't throw.

**TypeScript**

Types vanish at runtime, so drift shows up as `NaN`, `"undefined"` in the UI, or `.map is not a function`.
Validate at the boundary with zod (`z.coerce.number()`, `.catch()`, `.nullable()`), and use
`z.enum([...]).or(z.string())` for open enums.
