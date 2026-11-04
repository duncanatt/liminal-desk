import json
import unittest
from PySide6.QtCore import QCoreApplication, QObject, Signal
from liminal_desk.trace import Replay, Step, TraceError, parse_trace

APP = QCoreApplication.instance() or QCoreApplication([])


class FakeTransport(QObject):
    sent = Signal(int)
    state_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.state = 'connected'
        self.busy = False
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        self.busy = True

    def complete(self):
        self.busy = False
        self.sent.emit(len(self.payloads[-1]))

    def disconnect(self):
        self.state = 'disconnected'
        self.state_changed.emit(self.state)


class TraceParseTests(unittest.TestCase):
    def test_preserve_payload_tokens_and_any_json_value(self):
        raw = '{"big":9007199254740993,"same":1e999,"same":-0.000,"s":"\\u0126🌍"}'
        source = '{"version":1,"steps":[{"type":"send","payload":' + raw + '},{"type":"send","payload":null},{"type":"delay","duration_ms":0}]}'
        steps = parse_trace(source)
        self.assertEqual(steps[0].payload, raw.encode())
        self.assertEqual(steps[1].payload, b'null')
        self.assertEqual(steps[2].duration_ms, 0)
        for value in ['true', '42', '"text"', '[1,2]', '{}']:
            trace = '{"version":1,"steps":[{"type":"send","payload":' + value + '}]}'
            self.assertEqual(parse_trace(trace)[0].payload, value.encode())

    def test_invalid_control_schema_and_locations(self):
        bad = [[], {'version':2,'steps':[{'type':'send','payload':None}]},
               {'version':True,'steps':[]}, {'version':1,'steps':[]},
               {'version':1,'steps':[{'type':'send'}]},
               {'version':1,'steps':[{'type':'unknown'}]},
               {'version':1,'steps':[{'type':'send','payload':1,'topic':'a'}]},
               {'version':1,'steps':[5]}, {'version':1,'steps':[{'payload':1}]},
               {'version':1,'steps':[{'type':'delay','duration_ms':value} for value in [0]], 'extra':1}]
        bad += [{'version':1,'steps':[{'type':'delay','duration_ms':value}]} for value in [-1, 0.5, True, None, '100']]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(TraceError):
                    parse_trace(json.dumps(value))
        for text in ['{"version":1,"version":1,"steps":[{"type":"send","payload":1}]}',
                     '{"version":1,"steps":[{"type":"send","type":"send","payload":1}]}',
                     '{"version":1,"steps":[{"type":"send","payload":NaN}]}']:
            with self.assertRaises(TraceError):
                parse_trace(text)
        with self.assertRaises(TraceError) as caught:
            parse_trace('{\n "version":1,\n "steps":[{"type":"delay","duration_ms":-1}]\n}')
        self.assertEqual(caught.exception.diagnostic.line, 3)
        self.assertIn('Step 1', caught.exception.diagnostic.message)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.now = 0
        self.replay = Replay(self.transport, clock=lambda: self.now)

    def tearDown(self):
        self.replay.shutdown()

    def pump(self):
        APP.processEvents()

    def test_minimum_delay_pause_remaining_and_no_duplicate_send(self):
        self.replay.start([Step('send', b'1'), Step('delay', duration_ms=1000), Step('send', b'2')], self.transport.send)
        self.pump()
        self.assertEqual(self.transport.payloads, [b'1'])
        self.transport.complete()
        self.pump()
        self.assertEqual(self.replay.state, 'waiting')
        self.now += 400000000
        self.replay.tick()
        self.replay.pause()
        self.assertEqual(self.replay.remaining_ns, 600000000)
        self.now += 10000000000
        self.replay.resume()
        self.now += 599000000
        self.replay.tick()
        self.pump()
        self.assertEqual(self.transport.payloads, [b'1'])
        self.now += 1000000
        self.replay.tick()
        self.pump()
        self.assertEqual(self.transport.payloads, [b'1', b'2'])
        self.transport.complete()
        self.pump()
        self.assertEqual(self.replay.state, 'completed')

    def test_pause_and_stop_during_inflight_send(self):
        self.replay.start([Step('send', b'1'), Step('send', b'2'), Step('send', b'3')], self.transport.send)
        self.pump()
        self.replay.pause()
        self.assertEqual(self.replay.state, 'pausing')
        self.transport.complete()
        self.pump()
        self.assertEqual(self.replay.state, 'paused')
        self.assertEqual(self.transport.payloads, [b'1'])
        self.replay.resume()
        self.pump()
        self.replay.stop()
        self.assertEqual(self.replay.state, 'stopping')
        self.transport.complete()
        self.pump()
        self.assertEqual(self.replay.state, 'stopped')
        self.assertEqual(self.transport.payloads, [b'1', b'2'])

    def test_disconnect_while_paused_fails_and_explicit_restart_starts_first(self):
        self.replay.start([Step('delay', duration_ms=100), Step('send', b'1')], self.transport.send)
        self.pump()
        self.replay.pause()
        self.transport.disconnect()
        self.assertEqual(self.replay.state, 'error')
        self.now += 1000000000
        self.pump()
        self.assertEqual(self.transport.payloads, [])
        self.transport.state = 'connected'
        self.replay.start([Step('send', b'first')], self.transport.send)
        self.pump()
        self.assertEqual(self.transport.payloads, [b'first'])

    def test_pause_before_first_step_and_stop_during_wait(self):
        self.replay.start([Step('delay', duration_ms=1000), Step('send', b'1')], self.transport.send)
        self.replay.pause()
        self.pump()
        self.assertEqual(self.replay.state, 'paused')
        self.replay.resume()
        self.pump()
        self.assertEqual(self.replay.state, 'waiting')
        self.replay.stop()
        self.now += 10000000000
        self.replay.tick()
        self.pump()
        self.assertEqual(self.replay.state, 'stopped')
        self.assertEqual(self.transport.payloads, [])
        self.assertFalse(self.replay.timer.isActive())

    def test_zero_adjacent_delays_sender_failure_and_shutdown(self):
        self.replay.start([Step('delay', duration_ms=0), Step('delay', duration_ms=20), Step('send', b'1')], self.transport.send)
        for _ in range(3):
            self.pump()
        self.assertEqual(self.replay.state, 'waiting')
        self.now = 19999999
        self.replay.tick()
        self.pump()
        self.assertEqual(self.transport.payloads, [])
        self.now += 1
        self.replay.tick()
        self.pump()
        self.assertEqual(self.transport.payloads, [b'1'])
        self.replay.shutdown()
        self.assertFalse(self.replay.timer.isActive())
        self.assertFalse(self.replay.advance.isActive())
        def broken(_):
            raise OSError('write failed')
        self.transport.busy = False
        self.replay.start([Step('send', b'1')], broken)
        self.pump()
        self.assertEqual(self.replay.state, 'error')
