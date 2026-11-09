#!/usr/bin/env python3
"""
json_drift.py - detect drift between an API's JSON response and its expected contract.

Zero dependencies (Python 3.8+). PyYAML is used only if you pass a YAML OpenAPI spec.

Commands
  compare   Compare actual response(s) against a baseline (snapshot, JSON Schema, or OpenAPI).
  infer     Build a JSON Schema contract from one or more sample responses.

Examples
  python json_drift.py compare --baseline contracts/user.schema.json --actual response.json
  curl -s $API/users/1 -H "Authorization: Bearer $TOKEN" | python json_drift.py compare --baseline old.json -
  python json_drift.py compare --baseline openapi.json --openapi-path /users/{id} --method get --actual r.json
  python json_drift.py infer --out contracts/user.schema.json sample1.json sample2.json

Exit codes
  0  no drift at or above --fail-on level
  1  drift found at or above --fail-on level
  2  usage / input error
"""
import argparse
import difflib
import json
import re
import sys
from collections import Counter

SEVERITY_ORDER = {"breaking": 3, "warning": 2, "info": 1}
MAX_TRACKED_VALUES = 50

# --------------------------------------------------------------------------------------
# String format detection
# --------------------------------------------------------------------------------------
FORMAT_PATTERNS = [
    ("datetime", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")),
    ("date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("time", re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\s?[AaPp][Mm])?$")),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")),
    ("numeric", re.compile(r"^-?\d+(\.\d+)?$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("uri", re.compile(r"^https?://\S+$")),
]
SCHEMA_FORMAT_MAP = {
    "date-time": "datetime", "date": "date", "time": "time", "uuid": "uuid",
    "email": "email", "uri": "uri", "url": "uri",
}
ANNOTATION_KEYS = {"$schema", "$id", "$defs", "definitions", "title", "description", "examples",
                   "example", "$comment", "deprecated", "readOnly", "writeOnly", "default"}
INFER_FORMAT_MAP = {"datetime": "date-time", "date": "date", "time": "time",
                    "uuid": "uuid", "email": "email", "uri": "uri"}


def detect_format(s):
    if s == "":
        return "empty"
    for name, pat in FORMAT_PATTERNS:
        if pat.match(s):
            return name
    return "text"


def json_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def short(v, n=40):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------------------
# Shape model: one structure for samples, JSON Schema and OpenAPI so comparison is uniform
# --------------------------------------------------------------------------------------
class Shape:
    def __init__(self, source="sample"):
        self.source = source            # "sample" or "schema"
        self.types = set()              # json types; "any" = unconstrained
        self.props = {}                 # key -> Shape
        self.obj_count = 0              # how many objects observed (samples)
        self.key_counts = Counter()     # key -> times present (samples)
        self.required = None            # explicit required set (schema)
        self.additional = True          # additionalProperties
        self.items = None               # Shape for array items
        self.empty_array = False
        self.nonempty_array = False
        self.formats = set()            # detected (samples) or declared (schema) formats
        self.format_examples = {}
        self.enum = None                # set of json-dumped values (schema)
        self.values = set()             # json-dumped scalar values seen (samples, capped)
        self.examples = {}              # type -> example value

    def required_keys(self):
        if self.required is not None:
            return set(self.required)
        return {k for k, c in self.key_counts.items() if c == self.obj_count}


def add_sample(shape, v):
    t = json_type(v)
    shape.types.add(t)
    shape.examples.setdefault(t, v if t not in ("object", "array") else None)
    if t in ("string", "integer", "number", "boolean") and len(shape.values) < MAX_TRACKED_VALUES:
        shape.values.add(json.dumps(v))
    if t == "string":
        f = detect_format(v)
        shape.formats.add(f)
        shape.format_examples.setdefault(f, v)
    elif t == "array":
        if not v:
            shape.empty_array = True
        else:
            shape.nonempty_array = True
            if shape.items is None:
                shape.items = Shape("sample")
            for item in v:
                add_sample(shape.items, item)
    elif t == "object":
        shape.obj_count += 1
        for k, val in v.items():
            shape.key_counts[k] += 1
            if k not in shape.props:
                shape.props[k] = Shape("sample")
            add_sample(shape.props[k], val)


def shape_from_samples(samples):
    s = Shape("sample")
    for v in samples:
        add_sample(s, v)
    return s


# ---- JSON Schema / OpenAPI -----------------------------------------------------------
def resolve_ref(ref, root):
    if not ref.startswith("#/"):
        return None
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def merge_into(dst, src, required_mode):
    """Merge src Shape into dst. required_mode: 'union' (allOf) or 'intersect' (oneOf/anyOf)."""
    dst.types |= src.types
    for k, p in src.props.items():
        if k in dst.props:
            merge_into(dst.props[k], p, required_mode)
        else:
            dst.props[k] = p
    sr = src.required or set()
    if dst.required is None:
        dst.required = set(sr)
    elif required_mode == "union":
        dst.required |= sr
    else:
        dst.required &= sr
    dst.additional = dst.additional and src.additional
    if src.items is not None:
        if dst.items is None:
            dst.items = src.items
        else:
            merge_into(dst.items, src.items, required_mode)
    dst.formats |= src.formats
    if src.enum is not None:
        dst.enum = (dst.enum or set()) | src.enum


def shape_from_schema(schema, root, stack=()):
    s = Shape("schema")
    if not isinstance(schema, dict):
        s.types.add("any")
        return s
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in stack:  # recursive structure: stop descending
            s.types.add("any")
            return s
        target = resolve_ref(ref, root)
        if target is None:
            print(f"warning: could not resolve $ref {ref}; treating as unconstrained", file=sys.stderr)
            s.types.add("any")
            return s
        return shape_from_schema(target, root, stack + (ref,))

    for comb, mode in (("allOf", "union"), ("oneOf", "intersect"), ("anyOf", "intersect")):
        if comb in schema:
            parts = [shape_from_schema(x, root, stack) for x in schema[comb]]
            base = Shape("schema")
            first = True
            for p in parts:
                if first and mode == "intersect":
                    base.required = set(p.required or set())
                merge_into(base, p, mode)
                first = False
            rest = {k: v for k, v in schema.items() if k != comb and k not in ANNOTATION_KEYS}
            if rest:
                merge_into(base, shape_from_schema(rest, root, stack), "union")
            if mode == "union" and len(base.types) > 1:
                base.types.discard("any")  # an unconstrained allOf part doesn't loosen the others
            return base

    t = schema.get("type")
    if isinstance(t, list):
        s.types |= set(t)
    elif isinstance(t, str):
        s.types.add(t)
    else:
        if "properties" in schema:
            s.types.add("object")
        elif "items" in schema:
            s.types.add("array")
    if schema.get("nullable") is True:
        s.types.add("null")
    if "number" in s.types:
        s.types.add("integer")  # integer is a valid number
    for key in ("enum", "const"):
        if key in schema:
            vals = schema[key] if key == "enum" else [schema[key]]
            s.enum = {json.dumps(v) for v in vals}
            if not t:  # enum without an explicit type: take types from the values
                s.types |= {json_type(v) for v in vals}
    fmt = schema.get("format")
    if fmt in SCHEMA_FORMAT_MAP:
        s.formats = {SCHEMA_FORMAT_MAP[fmt]}
    if "properties" in schema or "object" in s.types:
        s.required = set(schema.get("required", []))
        if schema.get("additionalProperties") is False:
            s.additional = False
        for k, sub in (schema.get("properties") or {}).items():
            s.props[k] = shape_from_schema(sub, root, stack)
    if "items" in schema:
        s.items = shape_from_schema(schema["items"], root, stack)
    if not s.types:
        s.types.add("any")
    return s


def openapi_response_schema(spec, path, method, status):
    paths = spec.get("paths", {})
    if path not in paths:
        raise SystemExit(f"error: path {path!r} not in spec. Available: {', '.join(sorted(paths)[:30])}")
    op = paths[path].get(method.lower())
    if not op:
        raise SystemExit(f"error: method {method.upper()} not defined for {path}")
    responses = op.get("responses", {})
    resp = responses.get(str(status)) or responses.get(status) or responses.get("default")
    if resp is None:
        raise SystemExit(f"error: no {status} response for {method.upper()} {path}")
    if "$ref" in resp:
        resp = resolve_ref(resp["$ref"], spec) or {}
    if "content" in resp:  # OpenAPI 3
        content = resp["content"]
        media = next((m for m in content if "json" in m), None)
        if media is None:
            raise SystemExit(f"error: no JSON content type in {status} response")
        return content[media].get("schema", {})
    if "schema" in resp:  # Swagger 2
        return resp["schema"]
    raise SystemExit("error: response has no schema")


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------
def compile_ignores(patterns):
    out = []
    for p in patterns:
        rx = re.escape(p).replace(r"\*\*", ".*").replace(r"\*", r"[^.]*")
        out.append(re.compile("^" + rx + "$"))
    return out


def join(path, key):
    return key if path == "$" else f"{path}.{key}"


def norm_key(k):
    return re.sub(r"[_\-\s]", "", k).lower()


def type_label(types):
    t = set(types)
    if "integer" in t and "number" in t:
        t.discard("integer")
    return "|".join(sorted(t)) or "?"


class Comparer:
    def __init__(self, ignores):
        self.ignores = ignores
        self.findings = []
        self.checked_paths = 0

    def ignored(self, path):
        return any(rx.match(path) for rx in self.ignores)

    def add(self, severity, path, kind, message, expected="", actual="", hint=""):
        self.findings.append(dict(severity=severity, path=path, kind=kind, message=message,
                                  expected=expected, actual=actual, hint=hint))

    def compare(self, exp, act, path="$"):
        if self.ignored(path):
            return
        self.checked_paths += 1
        if "any" in exp.types:
            return
        if exp.source == "sample" and exp.types == {"null"}:
            if act.types - {"null"}:
                self.add("info", path, "unverified", "Baseline only had null here, so the type can't be checked",
                         "null", type_label(act.types))
            return

        field = path.split(".")[-1].replace("[]", "") if path != "$" else "value"
        new_types = set(act.types) - set(exp.types)
        if "integer" in new_types and "number" in exp.types:
            new_types.discard("integer")
        if "null" in new_types:
            new_types.discard("null")
            self.add("breaking", path, "became_nullable", "Value is now null where the contract doesn't allow null",
                     type_label(exp.types), "null",
                     "Typed clients (Dart `String`, TS non-optional) crash on null. Return a default, or mark the field nullable and update clients.")
        for t in sorted(new_types):
            ex = act.examples.get(t)
            if t == "number" and "integer" in exp.types:
                # A single snapshot with 12 can't prove the field is integer-only, so be softer there.
                sev = "breaking" if exp.source == "schema" else "warning"
                self.add(sev, path, "int_to_decimal", "Integer field now carries decimals",
                         "integer", f"number (e.g. {short(ex)})",
                         "Dart `as int` throws on 1.5. Keep integers integral, or type the client field as num/double."
                         + ("" if sev == "breaking" else " (Baseline snapshot only had whole numbers; confirm the intended type.)"))
            elif t == "string" and exp.types & {"integer", "number"} and act.formats <= {"numeric"}:
                self.add("breaking", path, "number_as_string", "Number is now serialized as a string",
                         type_label(exp.types), f"string (e.g. {short(ex)})",
                         f"Laravel: cast it ('{field}' => '{'integer' if exp.types == {'integer'} else 'float'}') in $casts or the API Resource; 'decimal:N' casts and some DB drivers produce strings.")
            elif t == "array" and "object" in exp.types and act.empty_array and not act.nonempty_array:
                self.add("breaking", path, "empty_object_as_array", "Empty object is serialized as []",
                         "object", "[] (empty array)",
                         "PHP json_encode turns an empty array into []. Return (object)[] / new \\stdClass, or use JSON_FORCE_OBJECT for that value.")
            elif t == "integer" and "boolean" in exp.types and act.values <= {"0", "1"}:
                self.add("breaking", path, "bool_as_int", "Boolean is now sent as 0/1",
                         "boolean", f"integer (e.g. {short(ex)})",
                         f"MySQL tinyint without a cast. Laravel: add '{field}' => 'boolean' to $casts.")
            elif t == "string" and "boolean" in exp.types and act.values <= {'"0"', '"1"', '"true"', '"false"'}:
                self.add("breaking", path, "bool_as_string", "Boolean is now sent as a string",
                         "boolean", f"string (e.g. {short(ex)})", "Cast to a real boolean on the API side.")
            elif t == "string" and "object" in exp.types:
                self.add("breaking", path, "object_flattened", "Nested object replaced by a string",
                         "object", f"string (e.g. {short(ex)})",
                         "Often a relation that is no longer eager-loaded or a Resource returning the raw attribute.")
            else:
                self.add("breaking", path, "type_changed", "Type changed",
                         type_label(exp.types), f"{t}" + (f" (e.g. {short(ex)})" if ex is not None else ""))

        if "object" in exp.types and "object" in act.types:
            self._compare_object(exp, act, path)
        if "array" in exp.types and "array" in act.types:
            self._compare_array(exp, act, path)
        if "string" in exp.types and "string" in act.types:
            self._compare_string(exp, act, path)
        if exp.enum is not None and act.values:
            new_vals = act.values - exp.enum
            if new_vals:
                self.add("warning", path, "new_enum_value", "Value(s) outside the declared enum",
                         ", ".join(sorted(exp.enum))[:80], ", ".join(sorted(new_vals))[:80],
                         "Clients that switch/map on this value need a fallback branch.")

    def _compare_object(self, exp, act, path):
        req = exp.required_keys()
        missing, partial, optional_absent = [], [], []
        for k, sub in exp.props.items():
            cp = join(path, k)
            if self.ignored(cp):
                continue
            present = act.key_counts.get(k, 0)
            if present == 0:
                if k in req:
                    missing.append(k)
                else:
                    optional_absent.append(k)
                continue
            if present < act.obj_count and k in req:
                partial.append((k, present))
            self.compare(sub, act.props[k], cp)
        added = [k for k in act.props if k not in exp.props and not self.ignored(join(path, k))]

        # rename detection: pair each missing key with the most similar added key
        renames = {}
        pool = list(added)
        for m in missing:
            best, best_score, casing = None, 0.0, False
            for a in pool:
                if norm_key(a) == norm_key(m):
                    best, best_score, casing = a, 1.0, True
                    break
                score = difflib.SequenceMatcher(None, m.lower(), a.lower()).ratio()
                compatible = bool((act.props[a].types - {"null"}) & (exp.props[m].types | {"any"})) or "any" in exp.props[m].types
                if compatible and score > best_score:
                    best, best_score = a, score
            if best and (casing or best_score >= 0.6):
                renames[m] = (best, casing)
                pool.remove(best)

        for m in missing:
            cp = join(path, m)
            if m in renames:
                new, casing = renames[m]
                if casing:
                    self.add("breaking", cp, "casing_changed", f"Key casing changed: `{m}` → `{new}`", m, new,
                             "Usually a camelCase/snake_case switch in a Resource or serializer. Keep the old key or update every client.")
                else:
                    self.add("breaking", cp, "field_renamed", f"Field looks renamed: `{m}` → `{new}`", m, new,
                             "Ship both keys for one release (old one deprecated) so older app versions keep working.")
                # still compare the renamed field's value shape
                self.compare(exp.props[m], act.props[new], join(path, new))
            else:
                self.add("breaking", cp, "field_removed", "Required field is missing", type_label(exp.props[m].types), "(absent)",
                         "Check $hidden, API Resource toArray(), whenLoaded()/when() conditions, or a select() that drops the column.")
        for k, present in partial:
            self.add("breaking", join(path, k), "sometimes_missing",
                     f"Field missing in {act.obj_count - present} of {act.obj_count} objects",
                     "always present", f"present in {present}/{act.obj_count}",
                     "Conditional output (whenLoaded, when, mergeWhen) or null-stripping. Always emit the key, with null if needed.")
        # Optional fields can legally be absent, but an exact casing twin appearing instead is a rename.
        for m in optional_absent:
            twin = next((a for a in pool if norm_key(a) == norm_key(m)), None)
            if twin:
                pool.remove(twin)
                renames[m] = (twin, True)
                self.add("warning", join(path, m), "casing_changed",
                         f"Optional field's key casing changed: `{m}` → `{twin}`", m, twin,
                         "Clients reading the old key silently get null/undefined. Keep the old key or update every client.")
                self.compare(exp.props[m], act.props[twin], join(path, twin))
        renamed_to = {v[0] for v in renames.values()}
        for k in added:
            if k in renamed_to:
                continue
            cp = join(path, k)
            if not exp.additional:
                self.add("warning", cp, "unexpected_field", "Field not allowed by the contract (additionalProperties: false)",
                         "(not allowed)", type_label(act.props[k].types))
            else:
                self.add("info", cp, "new_field", "New field (additive, normally safe)", "(absent)", type_label(act.props[k].types))

    def _compare_array(self, exp, act, path):
        cp = path + "[]"
        if exp.items is None:
            return
        if not act.nonempty_array:
            if exp.items.types and "any" not in exp.items.types:
                self.add("info", cp, "unverified", "Array was empty in the actual response; item shape not checked",
                         type_label(exp.items.types), "(no items)", "Use a response with at least one item for full coverage.")
            return
        if exp.source == "sample" and not exp.nonempty_array:
            self.add("info", cp, "unverified", "Baseline array was empty; item shape not checked", "(no items)",
                     type_label(act.items.types))
            return
        self.compare(exp.items, act.items, cp)

    def _compare_string(self, exp, act, path):
        if exp.source == "schema":
            enforce = bool(exp.formats)
        else:
            enforce = len(exp.formats) == 1 and next(iter(exp.formats)) not in ("text", "empty")
        if not enforce:
            return
        changed = act.formats - exp.formats
        if changed:
            exs = ", ".join(short(act.format_examples[f], 30) for f in sorted(changed))
            hint = "Dart DateTime.parse / JS Date parsing may fail or shift timezones." if exp.formats & {"datetime", "date"} else ""
            if "empty" in changed and exp.formats & {"datetime", "date"}:
                hint = "Empty string where a date is expected: return null instead of \"\"."
            self.add("warning", path, "format_changed", "String format changed",
                     "|".join(sorted(exp.formats)), f"{'|'.join(sorted(changed))} (e.g. {exs})", hint)


# --------------------------------------------------------------------------------------
# Baseline loading
# --------------------------------------------------------------------------------------
def load_json_or_yaml(path):
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if path.endswith((".yaml", ".yml")):
            try:
                import yaml  # type: ignore
            except ImportError:
                raise SystemExit("error: YAML spec needs PyYAML (pip install pyyaml) or convert it to JSON")
            return yaml.safe_load(text)
        raise SystemExit(f"error: {path} is not valid JSON: {e}")


def detect_baseline_type(doc):
    if isinstance(doc, dict):
        if "openapi" in doc or "swagger" in doc:
            return "openapi"
        if "$schema" in doc or ("type" in doc and isinstance(doc.get("type"), (str, list))
                                and ("properties" in doc or "items" in doc)):
            return "schema"
    return "snapshot"


def build_expected(args):
    doc = load_json_or_yaml(args.baseline)
    btype = args.baseline_type if args.baseline_type != "auto" else detect_baseline_type(doc)
    if btype == "openapi":
        if not args.openapi_path:
            raise SystemExit("error: OpenAPI baseline needs --openapi-path (and optionally --method, --status)")
        schema = openapi_response_schema(doc, args.openapi_path, args.method, args.status)
        return shape_from_schema(schema, doc), btype
    if btype == "schema":
        return shape_from_schema(doc, doc), btype
    return shape_from_samples([doc]), btype


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def summarize(findings):
    c = Counter(f["severity"] for f in findings)
    return {"breaking": c.get("breaking", 0), "warning": c.get("warning", 0), "info": c.get("info", 0)}


def md_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(findings, meta):
    s = summarize(findings)
    status = "❌ Breaking drift" if s["breaking"] else ("⚠️ Drift with warnings" if s["warning"] else "✅ No breaking drift")
    out = [f"# JSON drift report", "",
           f"**Result:** {status} — {s['breaking']} breaking, {s['warning']} warning, {s['info']} info",
           f"**Baseline:** `{meta['baseline']}` ({meta['baseline_type']})  ",
           f"**Actual:** {', '.join('`'+a+'`' for a in meta['actual'])}  ",
           f"**Paths checked:** {meta['checked_paths']}", ""]
    for sev, title in (("breaking", "Breaking"), ("warning", "Warnings"), ("info", "Info")):
        rows = [f for f in findings if f["severity"] == sev]
        if not rows:
            continue
        out += [f"## {title} ({len(rows)})", ""]
        if sev == "info":
            out += ["| Path | Change | Expected | Actual |", "|---|---|---|---|"]
            out += [f"| `{md_cell(f['path'])}` | {md_cell(f['message'])} | {md_cell(f['expected'])} | {md_cell(f['actual'])} |" for f in rows]
        else:
            out += ["| Path | Change | Expected | Actual | Likely fix |", "|---|---|---|---|---|"]
            out += [f"| `{md_cell(f['path'])}` | {md_cell(f['message'])} | {md_cell(f['expected'])} | {md_cell(f['actual'])} | {md_cell(f['hint'])} |" for f in rows]
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# infer: samples -> JSON Schema
# --------------------------------------------------------------------------------------
def shape_to_schema(s):
    types = sorted(s.types - {"null"}) or ["null"]
    if "number" in types and "integer" in types:
        types.remove("integer")
    if "null" in s.types and types != ["null"]:
        types.append("null")
    sch = {"type": types[0] if len(types) == 1 else types}
    if "object" in s.types:
        sch["properties"] = {k: shape_to_schema(p) for k, p in s.props.items()}
        req = sorted(s.required_keys())
        if req:
            sch["required"] = req
    if "array" in s.types:
        sch["items"] = shape_to_schema(s.items) if s.items is not None else {}
    if "string" in s.types and len(s.formats) == 1:
        f = next(iter(s.formats))
        if f in INFER_FORMAT_MAP:
            sch["format"] = INFER_FORMAT_MAP[f]
    if s.types == {"null"}:
        sch = {"description": "only null observed in samples; type unknown"}
    return sch


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def cmd_compare(args):
    expected, btype = build_expected(args)
    samples = []
    for a in args.actual:
        doc = load_json_or_yaml(a)
        if args.unwrap:
            for part in args.unwrap.split("."):
                if not isinstance(doc, dict) or part not in doc:
                    raise SystemExit(f"error: --unwrap key {args.unwrap!r} not found in {a}")
                doc = doc[part]
        samples.append(doc)
    actual = shape_from_samples(samples)
    cmp_ = Comparer(compile_ignores(args.ignore or []))
    cmp_.compare(expected, actual)
    findings = sorted(cmp_.findings, key=lambda f: (-SEVERITY_ORDER[f["severity"]], f["path"]))
    meta = {"baseline": args.baseline, "baseline_type": btype, "actual": args.actual,
            "checked_paths": cmp_.checked_paths}
    if args.format == "json":
        text = json.dumps({"summary": summarize(findings), "meta": meta, "findings": findings},
                          indent=2, ensure_ascii=False)
    else:
        text = render_markdown(findings, meta)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(text)
    if args.fail_on == "never":
        return 0
    threshold = SEVERITY_ORDER[args.fail_on]
    return 1 if any(SEVERITY_ORDER[f["severity"]] >= threshold for f in findings) else 0


def cmd_infer(args):
    samples = [load_json_or_yaml(p) for p in args.samples]
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if args.title:
        schema["title"] = args.title
    schema.update(shape_to_schema(shape_from_samples(samples)))
    text = json.dumps(schema, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"schema written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Detect drift between API JSON responses and their contract.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="compare actual response(s) against a baseline")
    c.add_argument("--baseline", required=True, help="snapshot JSON, JSON Schema, or OpenAPI spec (JSON/YAML)")
    c.add_argument("--baseline-type", choices=["auto", "snapshot", "schema", "openapi"], default="auto")
    c.add_argument("--openapi-path", help="OpenAPI path template, e.g. /users/{id}")
    c.add_argument("--method", default="get")
    c.add_argument("--status", default="200")
    c.add_argument("--actual", nargs="+", default=None, help="actual response file(s); '-' for stdin")
    c.add_argument("actual_pos", nargs="*", help=argparse.SUPPRESS)
    c.add_argument("--unwrap", help="dot path to compare inside actual, e.g. data (when baseline describes only the inner payload)")
    c.add_argument("--ignore", action="append", help="path glob to skip, e.g. 'data[].updated_at' or 'meta.**' (repeatable)")
    c.add_argument("--format", choices=["md", "json"], default="md")
    c.add_argument("--fail-on", choices=["breaking", "warning", "info", "never"], default="breaking")
    c.add_argument("--out", help="write report to file instead of stdout")

    i = sub.add_parser("infer", help="build a JSON Schema contract from sample responses")
    i.add_argument("samples", nargs="+")
    i.add_argument("--out")
    i.add_argument("--title")

    args = p.parse_args(argv)
    if args.cmd == "compare":
        args.actual = (args.actual or []) + (args.actual_pos or [])
        if not args.actual:
            p.error("compare needs at least one actual response (--actual FILE or '-')")
        return cmd_compare(args)
    return cmd_infer(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(2)
        raise
