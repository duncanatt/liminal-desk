"""Receive-path integration tests using independent local wire peers."""
from pathlib import Path
import os
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from liminal_desk.window import MainWindow
from liminal_desk.receiving import ReceivedMessage
from liminal_desk.validation import tcp_frame
from smoke_qt import APP, available_port, wait_for
from wire_broker import WireBroker


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow(preferences_path=False)
        self.window.show()
        APP.processEvents()
        self.peers = []
        self.brokers = []

    def tearDown(self):
        self.window.tcp.listener.shutdown()
        self.window.tcp.transport.shutdown()
        self.window.mqtt.transport.shutdown()
        wait_for(lambda: not self.window.mqtt.transport.is_running(), 7)
        for peer in self.peers:
            peer.abort()
        for broker in self.brokers:
            broker.close()
        self.window.close()
        APP.processEvents()

    def listener(self, maximum=1048576):
        tab = self.window.tcp
        tab.listen_port.setValue(available_port())
        tab.receive_maximum.setText(str(maximum))
        tab.start_listener_button.click()
        self.assertEqual(tab.listener.state, 'listening')
        peer = self.peer(tab.listen_port.value())
        wait_for(lambda: tab.listener.state == 'connected')
        return tab, peer

    def peer(self, port):
        peer = QTcpSocket()
        self.peers.append(peer)
        peer.connectToHost('127.0.0.1', port)
        wait_for(lambda: peer.state() == QAbstractSocket.SocketState.ConnectedState)
        return peer

    def mqtt(self, **options):
        broker = WireBroker(**options)
        self.brokers.append(broker)
        tab = self.window.mqtt
        tab.port.setValue(broker.port)
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        return tab, broker

    def test_tcp_fragmentation_batching_invalid_payloads_and_eof(self):
        tab, peer = self.listener()
        original = tab.editor.toPlainText()
        payload = '{"message":"Ħello 🌍","value":1e999}'.encode()
        frame = tcp_frame(payload)
        for part in [frame[:1], frame[1:3], frame[3:7], frame[7:]]:
            peer.write(part)
            peer.flush()
            QTest.qWait(10)
        wait_for(lambda: tab.viewer.model.rowCount() == 1)
        self.assertEqual(tab.viewer.selected.raw, payload)
        self.assertIn('1e999', tab.viewer.detail.toPlainText())
        peer.write(tcp_frame(b'{bad}') + tcp_frame(b'\xff\xfe') + tcp_frame(b'') + tcp_frame(b'null'))
        peer.flush()
        wait_for(lambda: tab.viewer.model.rowCount() == 5)
        tab.viewer.table.selectRow(1)
        self.assertIn('JSON error:', tab.viewer.metadata.text())
        tab.viewer.table.selectRow(2)
        self.assertIn('Invalid UTF-8', tab.viewer.metadata.text())
        self.assertEqual(tab.viewer.detail.toPlainText(), 'ff fe')
        tab.viewer.copy_raw_button.click()
        self.assertEqual(bytes(APP.clipboard().mimeData().data('application/octet-stream')), b'\xff\xfe')
        tab.viewer.table.selectRow(0)
        tab.viewer.copy_raw_button.click()
        self.assertEqual(APP.clipboard().text(), payload.decode())
        tab.viewer.copy_formatted_button.click()
        self.assertIn('1e999', APP.clipboard().text())
        before = tab.viewer.detail.toPlainText()
        QTest.keyClick(tab.viewer.detail, Qt.Key.Key_Return)
        QTest.keyClick(tab.viewer.detail, Qt.Key.Key_Tab)
        QTest.keyClicks(tab.viewer.detail, 'oops')
        self.assertEqual(tab.viewer.detail.toPlainText(), before)
        # Enough frames to require several event-loop turns, followed by EOF.
        peer.write(tcp_frame(b'{"batch":true}') * 100)
        peer.disconnectFromHost()
        wait_for(lambda: tab.viewer.model.rowCount() == 105 and tab.listener.state == 'listening')
        self.assertEqual(tab.editor.toPlainText(), original)
        peer = self.peer(tab.listen_port.value())
        wait_for(lambda: tab.listener.state == 'connected')
        peer.write(b'\x00\x00')
        peer.disconnectFromHost()
        wait_for(lambda: tab.listener.state == 'listening')
        self.assertIn('incomplete frame', tab.output.toPlainText())

    def test_tcp_limits_second_peer_bind_error_and_independent_sender(self):
        tab, peer = self.listener(maximum=8)
        second = QTcpSocket()
        self.peers.append(second)
        second.connectToHost('127.0.0.1', tab.listen_port.value())
        wait_for(lambda: 'additional TCP producer' in tab.output.toPlainText())
        self.assertIsNotNone(tab.listener.peer)
        peer.write(struct.pack('>I', 0xFFFFFFFF))
        peer.flush()
        wait_for(lambda: tab.listener.state == 'listening')
        self.assertIn('receive limit is 8', tab.output.toPlainText())
        # A second listener cannot bind the active endpoint.
        from liminal_desk.receiving import TcpListener
        other = TcpListener()
        with self.assertRaises(ValueError):
            other.start('127.0.0.1', tab.listen_port.value())
        server = QTcpServer()
        self.assertTrue(server.listen(QHostAddress.SpecialAddress.LocalHost, 0))
        try:
            tab.port.setValue(server.serverPort())
            tab.connect_button.click()
            wait_for(lambda: tab.transport.state == 'connected')
            peer = self.peer(tab.listen_port.value())
            wait_for(lambda: tab.listener.state == 'connected')
            peer.write(tcp_frame(b'{}'))
            peer.flush()
            wait_for(lambda: tab.viewer.model.rowCount() == 1)
            tab.viewer.clear_button.click()
            self.assertEqual(tab.listener.state, 'connected')
            self.assertEqual(tab.transport.state, 'connected')
            tab.stop_listener_button.click()
            self.assertEqual(tab.transport.state, 'connected')
            self.assertFalse(tab.listener.server.isListening())
        finally:
            server.close()

    def test_unsubscribe_timeout_and_close_releases_all_receivers(self):
        tab, broker = self.mqtt(unsubscribe_ack=False)
        tab.transport.timeout_seconds = 0.25
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'active')
        tab.unsubscribe_button.click()
        wait_for(lambda: tab.transport.state == 'disconnected')
        self.assertIn('acknowledge subscription', tab.output.toPlainText())
        wait_for(lambda: not tab.transport.is_running())
        broker = WireBroker()
        self.brokers.append(broker)
        tab.port.setValue(broker.port)
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'active')
        tcp, peer = self.listener()
        self.window.close()
        wait_for(lambda: not self.window.isVisible() and not tab.transport.is_running())
        self.assertFalse(tcp.listener.server.isListening())
        self.assertIsNone(tcp.listener.peer)
        self.assertEqual(tab.transport.subscription_state, 'idle')
        self.assertFalse(tab.transport.receive_timer.isActive())
        wait_for(lambda: peer.state() == QAbstractSocket.SocketState.UnconnectedState)

    def test_viewer_eviction_preserves_selection_and_byte_limit(self):
        viewer = self.window.tcp.viewer
        for index in range(500):
            viewer.add_message(ReceivedMessage(str(index).encode(), 'peer'))
        viewer.table.selectRow(100)
        selected = viewer.selected
        viewer.add_message(ReceivedMessage(b'500', 'peer'))
        self.assertEqual(viewer.model.rowCount(), 500)
        self.assertIs(viewer.selected, selected)
        self.assertEqual(viewer.history.messages[0].raw, b'1')
        viewer.history.maximum_bytes = 10
        viewer.add_message(ReceivedMessage(b'123456789', 'peer'))
        self.assertEqual(viewer.history.total_bytes, 9)
        self.assertEqual(viewer.model.rowCount(), 1)
        viewer.clear()
        self.assertFalse(viewer.copy_raw_button.isEnabled())

    def test_mqtt_subscribe_publish_unsubscribe_custom_filters_and_metadata(self):
        tab, broker = self.mqtt()
        self.assertTrue(tab.topic.isEditable())
        self.assertTrue(tab.subscribe_filter.isEditable())
        self.assertEqual(tab.subscribe_filter.text(), 'liminal/#')
        tab.subscribe_filter.setText('custom/+/imu')
        tab.subscribe_button.click()
        self.assertEqual(tab.transport.subscription_state, 'subscribing')
        self.assertFalse(tab.subscribe_filter.isEnabled())
        wait_for(lambda: tab.transport.subscription_state == 'active')
        broker.deliver('custom/device/imu', b'{"value":42}', retain=True)
        broker.deliver('other/device/imu', b'{}')
        wait_for(lambda: tab.viewer.model.rowCount() == 1)
        message = tab.viewer.history.messages[0]
        self.assertEqual((message.origin, message.qos, message.retained), ('custom/device/imu', 0, True))
        self.assertIn('Retained: yes', tab.viewer.metadata.text())
        self.assertTrue(tab.send_button.isEnabled())
        tab.topic.setText('custom/device/imu')
        tab.editor.setPlainText('{"sent":true}')
        tab.send_button.click()
        wait_for(lambda: tab.viewer.model.rowCount() == 2 and not tab.transport.busy)
        self.assertEqual(len(broker.connects), 1)
        tab.unsubscribe_button.click()
        self.assertEqual(tab.transport.subscription_state, 'unsubscribing')
        wait_for(lambda: tab.transport.subscription_state == 'idle')
        self.assertTrue(tab.subscribe_filter.isEnabled())
        broker.deliver('custom/device/imu', b'{"after":true}')
        QTest.qWait(120)
        self.assertEqual(tab.viewer.model.rowCount(), 2)
        tab.subscribe_filter.setText('bad/#/filter')
        tab.subscribe_button.click()
        self.assertIn('final topic level', tab.output.toPlainText())
        self.assertEqual(tab.transport.subscription_state, 'idle')
        tab.subscribe_filter.setText('liminal/#')
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'active')
        tab.disconnect_button.click()
        wait_for(lambda: tab.transport.state == 'disconnected' and not tab.transport.is_running())
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        self.assertEqual(tab.transport.subscription_state, 'idle')
        broker.deliver('liminal/test', b'{}')
        QTest.qWait(120)
        self.assertEqual(tab.viewer.model.rowCount(), 2)

    def test_mqtt_rejection_control_timeouts_and_oversize(self):
        tab, broker = self.mqtt(subscription_code=128)
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'rejected')
        self.assertIn('rejected subscription', tab.output.toPlainText())
        self.assertTrue(tab.send_button.isEnabled())
        tab.disconnect_button.click()
        wait_for(lambda: not tab.transport.is_running() and tab.transport.state == 'disconnected')
        broker = WireBroker(subscription_ack=False)
        self.brokers.append(broker)
        tab.port.setValue(broker.port)
        tab.transport.timeout_seconds = 0.25
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.state == 'disconnected')
        self.assertIn('acknowledge subscription', tab.output.toPlainText())
        wait_for(lambda: not tab.transport.is_running())
        broker = WireBroker()
        self.brokers.append(broker)
        tab.port.setValue(broker.port)
        tab.receive_maximum.setText('8')
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'active')
        broker.deliver('liminal/test', b'123456789')
        broker.deliver('liminal/test', b'{}')
        wait_for(lambda: tab.viewer.model.rowCount() == 1)
        self.assertEqual(tab.viewer.history.messages[0].raw, b'{}')
        wait_for(lambda: 'exceeding receive limit' in tab.output.toPlainText(), 2)
        self.assertEqual(tab.transport.state, 'connected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
