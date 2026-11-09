# Turning client models into a JSON Schema baseline

When the only "contract" is the code that parses the response, convert that code into a JSON Schema
and compare the API against it. This catches exactly what will crash the app.

Write the schema to a file (e.g. `/tmp/drift/client-contract.schema.json`) and run
`json_drift.py compare --baseline <that file> --actual <response>`.

## Rules for the conversion

1. Follow the parse code, not the class declaration alone. The JSON key is whatever `fromJson` reads
   (`json['created_at']`, `@JsonKey(name: 'created_at')`, `@SerializedName`), which may differ from the field name.
2. Required = the client reads the key without a fallback. Optional = it uses `??`, `containsKey`,
   `?.`, `@JsonKey(defaultValue: ...)`, or a nullable type that tolerates the key's absence.
3. Nullable = the Dart/TS type allows null (`String?`, `string | null`, `.nullable()`).
   A nullable-but-required key gets `"type": ["string", "null"]` and stays in `required`.
4. Nested models become nested schemas; `List<Order>` becomes `{"type": "array", "items": {...}}`.
5. Map client types: `int` → `integer`; `double` → `number`; `num` → `number`; `String` → `string`;
   `bool` → `boolean`; `DateTime` (parsed from string) → `{"type": "string", "format": "date-time"}`;
   `Map<String, dynamic>` → `{"type": "object"}`; `dynamic`/`Object?`/`any`/`unknown` → `{}`.
   Dart enums parsed by name → `{"type": "string", "enum": [...]}` (add `unknownEnumValue` only if the client has one: then omit `enum`).
6. Only include what the client actually reads. Extra API fields are harmless to the client and
   will show up as `info` anyway.

## Dart example

```dart
@JsonSerializable()
class User {
  final int id;
  final String name;
  @JsonKey(name: 'avatar_url') final String? avatarUrl;
  @JsonKey(name: 'is_active') final bool isActive;
  @JsonKey(name: 'created_at') final DateTime createdAt;
  final List<Order> orders;
  final Map<String, dynamic> settings;
}
```

```json
{
  "type": "object",
  "required": ["id", "name", "is_active", "created_at", "orders", "settings"],
  "properties": {
    "id": {"type": "integer"},
    "name": {"type": "string"},
    "avatar_url": {"type": ["string", "null"]},
    "is_active": {"type": "boolean"},
    "created_at": {"type": "string", "format": "date-time"},
    "orders": {"type": "array", "items": {"$ref": "#/$defs/Order"}},
    "settings": {"type": "object"}
  },
  "$defs": {"Order": {"type": "object", "required": ["id", "total"], "properties": {
    "id": {"type": "integer"}, "total": {"type": "number"}}}}
}
```

Note `avatar_url`: json_serializable treats a nullable field as optional, so it isn't required.
For hand-written `fromJson` with `json['avatar_url'] as String?`, a missing key is also fine (yields null).
But `json['name'] as String` throws on missing *or* null — required and non-nullable.

If the response is wrapped (`{"data": {...}}`) and the model parses only the inner object, either wrap
the schema the same way or run compare with `--unwrap data`.

## TypeScript / zod

- `interface User { id: number; name: string; avatar_url?: string | null }` → `id`, `name` required;
  `avatar_url` optional and nullable. `number` → `number` (TS can't distinguish int).
- zod: `z.object({...})` keys are required unless `.optional()`/`.default()`; `.nullable()` adds null;
  `z.coerce.number()` tolerates numeric strings (so you can treat `number_as_string` there as a warning, not breaking);
  `.strict()` → `"additionalProperties": false`; `z.enum([...])` → `enum`.

## Kotlin / Swift (quick map)

- Kotlin `kotlinx.serialization`: non-null without default → required; `@SerialName` gives the key.
- Swift `Codable`: non-optional property → required; `CodingKeys` gives the key; optional → may be missing or null.
