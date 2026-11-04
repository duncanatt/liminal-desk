import unittest
from liminal_desk.validation import subscription_filter
from liminal_desk.receiving import MessageHistory, ReceiveInbox, ReceivedMessage


class ReceivingTests(unittest.TestCase):
    def test_filter_grammar_preserves_topic_levels_and_spaces(self):
        for value in ['liminal/#', 'liminal/+/imu', 'liminal/sensors/esp32-001/imu', '#', '+', '/+/', ' a /#']:
            self.assertEqual(subscription_filter(value), value)
        for value in ['', 'a/#/b', 'a#', 'a/+b', 'a\x00', '\ud800', 'a' * 65536]:
            with self.subTest(value=value[:30]):
                with self.assertRaises(ValueError):
                    subscription_filter(value)

    def test_history_count_and_byte_limits(self):
        history = MessageHistory(3, 8)
        for _ in range(5):
            history.append(ReceivedMessage(b'{}', 'test'))
        self.assertEqual(len(history.messages), 3)
        self.assertEqual(history.total_bytes, 6)
        history.append(ReceivedMessage(b'1234567', 'test'))
        self.assertEqual(len(history.messages), 1)
        self.assertEqual(history.total_bytes, 7)
        history.clear()
        self.assertEqual(history.total_bytes, 0)

    def test_cross_thread_backlog_is_bounded(self):
        inbox = ReceiveInbox()
        for index in range(200):
            inbox.put(ReceivedMessage(str(index).encode(), 'test'))
        messages, dropped = inbox.take(128)
        self.assertEqual(len(messages), 128)
        self.assertEqual(dropped, 72)
        self.assertEqual(messages[0].raw, b'72')
        self.assertEqual(inbox.take(), ([], 0))
