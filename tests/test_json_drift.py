"""Run: python -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import json_drift as jd  # noqa: E402

EX = os.path.join(ROOT, "examples")
SCRIPT = os.path.join(ROOT, "scripts", "json_drift.py")


def kinds_for_samples(baseline, actual, ignores=()):
    c = jd.Comparer(jd.compile_ignores(list(ignores)))
    c.compare(jd.shape_from_samples([baseline]), jd.shape_from_samples([actual]))
    return {(f["path"], f["kind"], f["severity"]) for f in c.findings}


def kinds_for_schema(schema, actual):
    c = jd.Comparer([])
    c.compare(jd.shape_from_schema(schema, schema), jd.shape_from_samples([actual]))
    return {(f["path"], f["kind"], f["severity"]) for f in c.findings}


class SnapshotDrift(unittest.TestCase):
    def test_identical_is_clean(self):
        doc = {"a": 1, "b": {"c": "x"}, "d": [1, 2]}
        self.assertEqual(kinds_for_samples(doc, doc), set())

    def test_number_as_string(self):
        self.assertIn(("id", "number_as_string", "breaking"), kinds_for_samples({"id": 1}, {"id": "1"}))

    def test_became_nullable(self):
        self.assertIn(("email", "became_nullable", "breaking"), kinds_for_samples({"email": "a@b.co"}, {"email": None}))

    def test_bool_as_int(self):
        self.assertIn(("on", "bool_as_int", "breaking"), kinds_for_samples({"on": True}, {"on": 1}))

    def test_empty_object_as_array(self):
        self.assertIn(("s", "empty_object_as_array", "breaking"), kinds_for_samples({"s": {"k": 1}}, {"s": []}))

    def test_removed_and_new(self):
        k = kinds_for_samples({"keep": 1, "gone": 2}, {"keep": 1, "fresh": "x"})
        self.assertIn(("gone", "field_removed", "breaking"), k)
        self.assertIn(("fresh", "new_field", "info"), k)

    def test_casing_change(self):
        self.assertIn(("created_at", "casing_changed", "breaking"),
                      kinds_for_samples({"created_at": "2026-01-01"}, {"createdAt": "2026-01-01"}))

    def test_rename(self):
        self.assertIn(("name", "field_renamed", "breaking"), kinds_for_samples({"name": "A"}, {"fullName": "A"}))

    def test_format_change(self):
        self.assertIn(("t", "format_changed", "warning"),
                      kinds_for_samples({"t": "2026-01-10T08:30:00Z"}, {"t": "10/01/2026"}))

    def test_int_to_decimal_soft_in_snapshot(self):
        self.assertIn(("q", "int_to_decimal", "warning"), kinds_for_samples({"q": 2}, {"q": 2.5}))

    def test_integer_where_number_is_fine(self):
        self.assertEqual(kinds_for_samples({"p": 1.5}, {"p": 2}), set())

    def test_null_baseline_is_unverified_not_breaking(self):
        self.assertIn(("x", "unverified", "info"), kinds_for_samples({"x": None}, {"x": "hello"}))

    def test_array_items_and_sometimes_missing(self):
        base = {"items": [{"id": 1, "sku": "A"}]}
        act = {"items": [{"id": 1, "sku": "A"}, {"id": 2}]}
        self.assertIn(("items[].sku", "sometimes_missing", "breaking"), kinds_for_samples(base, act))

    def test_ignore_glob(self):
        base = {"meta": {"ts": 1}, "data": [{"updated_at": "2026-01-01T00:00:00Z"}]}
        act = {"meta": {"ts": "x"}, "data": [{"updated_at": "yesterday"}]}
        self.assertEqual(kinds_for_samples(base, act, ignores=["meta.**", "data[].updated_at"]), set())


class SchemaDrift(unittest.TestCase):
    def test_optional_missing_is_fine(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
        self.assertEqual(kinds_for_schema(schema, {"a": "x"}), set())

    def test_enum(self):
        schema = {"type": "object", "properties": {"s": {"type": "string", "enum": ["paid", "pending"]}}}
        self.assertIn(("s", "new_enum_value", "warning"), kinds_for_schema(schema, {"s": "refunded"}))

    def test_nullable_openapi_30(self):
        schema = {"type": "object", "properties": {"d": {"type": "string", "nullable": True}}}
        self.assertEqual(kinds_for_schema(schema, {"d": None}), set())

    def test_additional_properties_false(self):
        schema = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "integer"}}}
        self.assertIn(("x", "unexpected_field", "warning"), kinds_for_schema(schema, {"a": 1, "x": 2}))

    def test_refs_and_allof(self):
        schema = {
            "$defs": {"Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
            "allOf": [{"$ref": "#/$defs/Base"},
                      {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}],
        }
        k = kinds_for_schema(schema, {"id": "7"})
        self.assertIn(("id", "number_as_string", "breaking"), k)
        self.assertIn(("name", "field_removed", "breaking"), k)

    def test_recursive_ref_terminates(self):
        schema = {"$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
                  "$ref": "#/$defs/Node"}
        kinds_for_schema(schema, {"child": {"child": {"child": None}}})


class Cli(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin, capture_output=True, text=True)

    def test_exit_codes(self):
        bad = self.run_cli("compare", "--baseline", os.path.join(EX, "baseline-snapshot.json"),
                           os.path.join(EX, "actual-drifted.json"))
        self.assertEqual(bad.returncode, 1)
        ok = self.run_cli("compare", "--baseline", os.path.join(EX, "baseline-snapshot.json"),
                          os.path.join(EX, "actual-ok.json"))
        self.assertEqual(ok.returncode, 0)
        never = self.run_cli("compare", "--baseline", os.path.join(EX, "baseline-snapshot.json"),
                             os.path.join(EX, "actual-drifted.json"), "--fail-on", "never")
        self.assertEqual(never.returncode, 0)

    def test_openapi_stdin_json(self):
        with open(os.path.join(EX, "actual-drifted.json")) as fh:
            r = self.run_cli("compare", "--baseline", os.path.join(EX, "openapi.json"),
                             "--openapi-path", "/api/users/{id}", "--format", "json", "-", stdin=fh.read())
        d = json.loads(r.stdout)
        self.assertGreater(d["summary"]["breaking"], 0)
        self.assertIn("new_enum_value", {f["kind"] for f in d["findings"]})

    def test_infer_roundtrip(self):
        r = self.run_cli("infer", os.path.join(EX, "baseline-snapshot.json"))
        schema = json.loads(r.stdout)
        c = jd.Comparer([])
        with open(os.path.join(EX, "baseline-snapshot.json")) as fh:
            c.compare(jd.shape_from_schema(schema, schema), jd.shape_from_samples([json.load(fh)]))
        self.assertEqual([f for f in c.findings if f["severity"] != "info"], [])


if __name__ == "__main__":
    unittest.main()
