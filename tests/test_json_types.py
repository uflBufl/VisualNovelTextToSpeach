import json
import math
import sys
import unittest
from unittest.mock import patch

from vntts.json_types import decode_json, is_utf8_text


class JsonDecodeTest(unittest.TestCase):
    def test_utf8_text_admission_preserves_json_decoder_contract(self):
        for value in ("", "  text  ", "Привет", "🌊", "\x00"):
            with self.subTest(value=value):
                self.assertTrue(is_utf8_text(value))
        for value in (None, b"text", 1, [], {}, "\ud800", "\udcff", "\ud800\udc00"):
            with self.subTest(value=value):
                self.assertFalse(is_utf8_text(value))
        decoded = decode_json('"\\ud800"')
        self.assertEqual(decoded, "\ud800")
        self.assertFalse(is_utf8_text(decoded))

    def test_integer_digit_limit_is_a_parse_error_for_every_input_format(self):
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            payload = '{"value":' + "1" * 641 + "}"
            for value in (payload, payload.encode(), bytearray(payload.encode())):
                with self.subTest(format=type(value).__name__):
                    with self.assertRaises(json.JSONDecodeError) as caught:
                        decode_json(value)
                    self.assertIs(type(caught.exception.__cause__), ValueError)
                    self.assertEqual(
                        caught.exception.msg, str(caught.exception.__cause__)
                    )
                    self.assertEqual(sys.get_int_max_str_digits(), 640)
        finally:
            sys.set_int_max_str_digits(previous)
        self.assertEqual(sys.get_int_max_str_digits(), previous)

    def test_existing_json_unicode_and_type_errors_retain_identity(self):
        for error in (
            json.JSONDecodeError("bad JSON", "{", 1),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
            TypeError("unsupported input"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("vntts.json_types.json.loads", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        decode_json("{}")
                self.assertIs(caught.exception, error)

    def test_depth_limit_retains_existing_message_and_cause(self):
        error = RecursionError("native parser limit")
        with patch("vntts.json_types.json.loads", side_effect=error):
            with self.assertRaises(json.JSONDecodeError) as caught:
                decode_json("[]")
        self.assertEqual(caught.exception.msg, "JSON nesting exceeds the decoder limit")
        self.assertIs(caught.exception.__cause__, error)

    def test_valid_values_preserve_stdlib_decoding(self):
        payload = json.dumps(
            {
                "count": 10**100,
                "fraction": 1.25,
                "label": "Привет",
                "items": [None, True],
            }
        )
        for value in (
            payload,
            payload.encode(),
            bytearray(payload.encode()),
            payload.encode("utf-16"),
        ):
            with self.subTest(format=type(value).__name__):
                self.assertEqual(decode_json(value), json.loads(value))

    def test_legacy_nonfinite_scalars_remain_accepted_before_domain_validation(self):
        self.assertTrue(math.isnan(decode_json("NaN")))
        self.assertEqual(decode_json("Infinity"), float("inf"))
