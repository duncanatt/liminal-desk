"""Strict JSON and transport validation, independent of the interface."""
import json
import re
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    message: str
    position: int
    line: int
    column: int


def validate_json(text):
    try:
        # Python accepts these non-JSON constants unless explicitly rejected.
        def reject(value):
            raise ValueError(value)
        value = json.loads(text, parse_constant=reject)
        # An escaped lone surrogate is unusable as a Unicode JSON string.
        def check(item):
            if isinstance(item, str):
                item.encode('utf-8')
            elif isinstance(item, list):
                for entry in item:
                    check(entry)
            elif isinstance(item, dict):
                for key, entry in item.items():
                    check(key)
                    check(entry)
        check(value)
        text.encode('utf-8')
        return None
    except json.JSONDecodeError as exc:
        return Diagnostic(exc.msg, exc.pos, exc.lineno, exc.colno)
    except (UnicodeError, RecursionError):
        return Diagnostic('Payload contains invalid Unicode or is nested too deeply', 0, 1, 1)
    except ValueError as exc:
        # Locate a non-standard constant outside quoted strings.
        for match in re.finditer(r'"(?:\\.|[^"\\])*"|(-?Infinity|NaN)', text):
            if match.group(1):
                pos = match.start()
                break
        else:
            pos = 0
        return Diagnostic(f'{exc} is not valid JSON', pos,
                          text.count('\n', 0, pos) + 1,
                          pos - text.rfind('\n', 0, pos))


def json_bytes(text):
    error = validate_json(text)
    if error:
        raise ValueError(f'{error.message} (line {error.line}, column {error.column})')
    return text.encode('utf-8')


def endpoint(host, port):
    host = host.strip()
    if not host or any(c.isspace() for c in host) or '://' in host or any(c in host for c in '/\x00'):
        raise ValueError('Enter a hostname or IP address, without a URL scheme or path.')
    if not 1 <= int(port) <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    return host, int(port)


def topic_name(topic):
    if not topic or any(c in topic for c in '+#\x00'):
        raise ValueError('Enter a publish topic without +, #, or null characters.')
    try:
        size = len(topic.encode('utf-8'))
    except UnicodeError as exc:
        raise ValueError('Topic contains invalid Unicode.') from exc
    if size > 65535:
        raise ValueError('Topic cannot exceed 65,535 UTF-8 bytes.')
    return topic


def tcp_frame(payload, maximum=1048576):
    if not 1 <= maximum <= 0xFFFFFFFF:
        raise ValueError('Maximum payload must be between 1 and 4,294,967,295 bytes.')
    if len(payload) > maximum:
        raise ValueError(f'Payload is {len(payload):,} bytes; limit is {maximum:,} bytes.')
    return struct.pack('>I', len(payload)) + payload


def subscription_filter(value):
    if not value or '\x00' in value:
        raise ValueError('Enter a non-empty subscription filter without null characters.')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise ValueError('Subscription filter contains invalid Unicode.') from exc
    if size > 65535:
        raise ValueError('Subscription filter cannot exceed 65,535 UTF-8 bytes.')
    levels = value.split('/')
    for index, level in enumerate(levels):
        if '+' in level and level != '+':
            raise ValueError('+ must occupy an entire topic level.')
        if '#' in level and (level != '#' or index != len(levels) - 1):
            raise ValueError('# must occupy the final topic level.')
    return value
