"""Versioned trace parsing and asynchronous, transport-independent replay."""
from dataclasses import dataclass
import json
import re
import time

from PySide6.QtCore import QObject, QTimer, Signal
from .editor import JsonEditor
from .validation import Diagnostic, validate_json


class TraceError(ValueError):
    def __init__(self, text, message, position=0):
        super().__init__(message)
        self.diagnostic = Diagnostic(message, position, text.count('\n', 0, position) + 1,
                                     position - text.rfind('\n', 0, position))


@dataclass(frozen=True)
class Step:
    kind: str
    payload: bytes = b''
    duration_ms: int = 0


@dataclass
class Node:
    start: int
    end: int
    kind: str
    children: object = None


def parse_trace(text):
    diagnostic = validate_json(text)
    if diagnostic:
        raise TraceError(text, diagnostic.message, diagnostic.position)
    decoder = json.JSONDecoder(parse_float=str, parse_int=str)

    def space(pos):
        while pos < len(text) and text[pos] in ' \t\r\n':
            pos += 1
        return pos

    def node(pos):
        pos = space(pos)
        start = pos
        if text[pos] == '{':
            children = []
            pos = space(pos + 1)
            while text[pos] != '}':
                key_start = pos
                key, pos = decoder.raw_decode(text, pos)
                pos = space(pos)
                child = node(pos + 1)
                children.append((key, key_start, child))
                pos = space(child.end)
                if text[pos] == '}':
                    break
                pos = space(pos + 1)
            return Node(start, pos + 1, 'object', children)
        if text[pos] == '[':
            children = []
            pos = space(pos + 1)
            while text[pos] != ']':
                child = node(pos)
                children.append(child)
                pos = space(child.end)
                if text[pos] == ']':
                    break
                pos = space(pos + 1)
            return Node(start, pos + 1, 'array', children)
        _, end = decoder.raw_decode(text, pos)
        return Node(start, end, 'scalar')

    def fields(item, expected, context):
        if item.kind != 'object':
            raise TraceError(text, context + ' must be an object', item.start)
        result = {}
        for key, start, child in item.children:
            if key in result:
                raise TraceError(text, context + ': duplicate control field ' + key, start)
            if key not in expected:
                raise TraceError(text, context + ': unsupported field ' + key, start)
            result[key] = child
        missing = expected - result.keys()
        if missing:
            raise TraceError(text, context + ': missing ' + ', '.join(sorted(missing)), item.start)
        return result

    try:
        root = node(0)
    except RecursionError:
        raise TraceError(text, 'Trace is nested too deeply') from None
    top = fields(root, {'version', 'steps'}, 'Trace')
    version = top['version']
    if text[version.start:version.end] != '1':
        raise TraceError(text, 'Unsupported trace version; expected integer 1', version.start)
    steps = top['steps']
    if steps.kind != 'array' or not steps.children:
        raise TraceError(text, 'steps must be a non-empty array', steps.start)
    result = []
    for index, item in enumerate(steps.children):
        context = f'Step {index + 1}'
        if item.kind != 'object':
            raise TraceError(text, context + ' must be an object', item.start)
        types = [child for key, _, child in item.children if key == 'type']
        if not types:
            raise TraceError(text, context + ': missing type', item.start)
        kind = json.loads(text[types[0].start:types[0].end])
        if kind not in ('send', 'delay'):
            raise TraceError(text, context + ': type must be send or delay', types[0].start)
        expected = {'type', 'payload'} if kind == 'send' else {'type', 'duration_ms'}
        values = fields(item, expected, context)
        if kind == 'send':
            payload = values['payload']
            result.append(Step('send', text[payload.start:payload.end].encode('utf-8')))
        else:
            duration = values['duration_ms']
            raw = text[duration.start:duration.end]
            if not re.fullmatch(r'-?(?:0|[1-9]\d*)', raw) or int(raw) < 0:
                raise TraceError(text, context + ': duration_ms must be a non-negative integer', duration.start)
            result.append(Step('delay', duration_ms=int(raw)))
    return tuple(result)


class TraceEditor(JsonEditor):
    def diagnose(self, text):
        try:
            parse_trace(text)
            return None
        except TraceError as exc:
            return exc.diagnostic


class Replay(QObject):
    changed = Signal()
    activity = Signal(str)
    failed = Signal(str)
    active_states = {'running', 'waiting', 'pausing', 'paused', 'stopping'}

    def __init__(self, transport, parent=None, clock=time.monotonic_ns):
        super().__init__(parent)
        self.transport = transport
        self.clock = clock
        self.state = 'idle'
        self.steps = ()
        self.index = 0
        self.inflight = False
        self.remaining_ns = None
        self.deadline = None
        self.sender = None
        self.progress = 'Ready'
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.advance = QTimer(self)
        self.advance.setSingleShot(True)
        self.advance.timeout.connect(self._next)
        transport.sent.connect(self._sent)
        transport.state_changed.connect(self._connection)

    @property
    def active(self):
        return self.state in self.active_states

    def _set(self, state, progress):
        self.state, self.progress = state, progress
        self.changed.emit()

    def start(self, steps, sender):
        if self.active:
            raise ValueError('A trace is already active')
        if not steps or self.transport.state != 'connected' or self.transport.busy:
            raise ValueError('Connect an idle sender before running a trace')
        self.steps, self.sender = tuple(steps), sender
        self.index = 0
        self.inflight = False
        self.remaining_ns = None
        self.activity.emit(f'Trace started: {len(steps)} steps.')
        self._set('running', 'Starting')
        self.advance.start(0)

    def _next(self):
        if not self.active or self.state in ('paused', 'pausing', 'stopping'):
            return
        if self.index >= len(self.steps):
            self.activity.emit('Trace completed; local transmissions finished, Liminal processing unconfirmed.')
            self._set('completed', 'Completed')
            return
        step = self.steps[self.index]
        if step.kind == 'send':
            self.inflight = True
            self._set('running', f'Step {self.index + 1}/{len(self.steps)} · Sending')
            self.activity.emit(f'Trace step {self.index + 1}: sending {len(step.payload):,} bytes.')
            try:
                self.sender(step.payload)
            except Exception as exc:
                self.fail(str(exc))
        else:
            self.remaining_ns = step.duration_ms * 1000000
            self.deadline = self.clock() + self.remaining_ns
            self.activity.emit(f'Trace step {self.index + 1}: waiting {step.duration_ms:,} ms.')
            self._set('waiting', '')
            self.tick()
            if self.state == 'waiting':
                self.timer.start()

    def tick(self):
        if self.state != 'waiting':
            return
        self.remaining_ns = max(0, self.deadline - self.clock())
        if not self.remaining_ns:
            self.timer.stop()
            self.remaining_ns = None
            self.index += 1
            self._set('running', f'Step {self.index}/{len(self.steps)} · Delay finished')
            self.advance.start(0)
        else:
            ms = (self.remaining_ns + 999999) // 1000000
            self.progress = f'Step {self.index + 1}/{len(self.steps)} · Waiting {ms:,} ms'
            self.changed.emit()

    def _sent(self, _):
        if not self.active or not self.inflight:
            return
        self.inflight = False
        self.activity.emit(f'Trace step {self.index + 1}: local send completed.')
        self.index += 1
        if self.state == 'stopping':
            self._finish_stop()
        elif self.state == 'pausing':
            self._set('paused', f'Paused · {self.index}/{len(self.steps)} steps complete')
        else:
            self.advance.start(0)

    def pause(self):
        if self.state not in ('running', 'waiting'):
            return
        self.advance.stop()
        if self.inflight:
            self._set('pausing', f'Step {self.index + 1}/{len(self.steps)} · Pausing after send')
        else:
            if self.state == 'waiting':
                self.remaining_ns = max(0, self.deadline - self.clock())
                self.timer.stop()
            self._set('paused', f'Paused · Step {min(self.index + 1, len(self.steps))}/{len(self.steps)}')
        self.activity.emit('Trace pause requested.')

    def resume(self):
        if self.state != 'paused':
            return
        self.activity.emit('Trace resumed.')
        if self.remaining_ns is not None:
            self.deadline = self.clock() + self.remaining_ns
            self._set('waiting', '')
            self.tick()
            if self.state == 'waiting':
                self.timer.start()
        else:
            self._set('running', 'Resuming')
            self.advance.start(0)

    def stop(self):
        if not self.active:
            return
        self.timer.stop()
        self.advance.stop()
        self.remaining_ns = None
        if self.inflight:
            self._set('stopping', f'Stopping · Step {self.index + 1} already in flight')
            self.activity.emit('Trace stopped scheduling steps; waiting for the in-flight send outcome.')
        else:
            self._finish_stop()

    def _finish_stop(self):
        self.inflight = False
        self._set('stopped', f'Stopped · {self.index}/{len(self.steps)} steps complete')
        self.activity.emit('Trace stopped. No remaining steps will be sent.')

    def fail(self, message):
        self.timer.stop()
        self.advance.stop()
        self.inflight = False
        self.remaining_ns = None
        detail = f'Step {min(self.index + 1, len(self.steps))}/{len(self.steps)}: {message}'
        self._set('error', 'Error · ' + detail)
        self.failed.emit(detail)
        self.activity.emit('ERROR: Trace stopped: ' + detail + '. No automatic retry.')

    def _connection(self, state):
        if self.active and state in ('disconnected', 'disconnecting'):
            if self.state == 'stopping':
                self._finish_stop()
            else:
                self.fail('Sending connection closed; delivery of an in-flight send may be uncertain')

    def shutdown(self):
        self.timer.stop()
        self.advance.stop()
        if self.active:
            self._finish_stop()
