import struct
import unittest

from liminal_desk.editor import pretty_json
from liminal_desk.validation import endpoint, json_bytes, tcp_frame, topic_name, validate_json


class ValidationTests(unittest.TestCase):
    def test_unicode_frame_counts_bytes_and_preserves_text(self):
        text = '{ "message": "Ħello 🌍" }\n'
        payload = json_bytes(text)
        frame = tcp_frame(payload)
        self.assertEqual(struct.unpack('>I', frame[:4])[0], len(text.encode('utf-8')))
        self.assertEqual(frame[4:], text.encode('utf-8'))
        self.assertGreater(len(payload), len(text))

    def test_strict_json_and_error_location(self):
        for text in ['{"a": NaN}', 'Infinity', '-Infinity', '{"x": 1,}', '', '{"x": "\\ud800"}']:
            with self.subTest(text=text):
                self.assertIsNotNone(validate_json(text))
                with self.assertRaises(ValueError):
                    json_bytes(text)
        error = validate_json('{\n  "x":\n}')
        self.assertEqual((error.line, error.column), (3, 1))
        self.assertIsNone(validate_json('{"value":1e999}'))
        for value in ['null', 'true', '[]', '"text"', '42']:
            self.assertIsNone(validate_json(value))

    def test_format_preserves_numeric_lexemes_and_duplicate_keys(self):
        text = '{"x":1e999,"x":-0.000,"emoji":"🌍","list":[true,null,{}]}'
        formatted = pretty_json(text)
        self.assertIn('1e999', formatted)
        self.assertIn('-0.000', formatted)
        self.assertEqual(formatted.count('"x"'), 2)
        self.assertIsNone(validate_json(formatted))

    def test_reject_bad_transport_settings_and_oversize(self):
        for host in ['', 'mqtt://localhost', 'some host', 'localhost/path']:
            with self.assertRaises(ValueError):
                endpoint(host, 1883)
        for port in [0, 65536]:
            with self.assertRaises(ValueError):
                endpoint('localhost', port)
        for topic in ['', '#', 'a/+', 'a\x00b']:
            with self.assertRaises(ValueError):
                topic_name(topic)
        with self.assertRaises(ValueError):
            tcp_frame(b'{}', 1)
        with self.assertRaises(ValueError):
            tcp_frame(b'{}', 2**32)


if __name__ == '__main__':
    unittest.main()
