"""Headless end-to-end UI, TCP, and MQTT checks using local wire peers."""
import os
from pathlib import Path
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtNetwork import QTcpServer, QHostAddress
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from liminal_desk.window import MainWindow
from wire_broker import WireBroker, packet, receive

APP = QApplication.instance() or QApplication([])


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError('Timed out waiting for network/UI state')


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow(preferences_path=False)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.tcp.transport.shutdown()
        self.window.mqtt.transport.shutdown()
        wait_for(lambda: not self.window.mqtt.transport.is_running(), 7)
        APP.processEvents()
        self.window.close()
        APP.processEvents()

    def tcp_server(self):
        server = QTcpServer()
        self.assertTrue(server.listen(QHostAddress.SpecialAddress.LocalHost, 0))
        tab = self.window.tcp
        tab.host.setText('127.0.0.1')
        tab.port.setValue(server.serverPort())
        return server

    def test_editor_diagnostics_and_controls(self):
        tab = self.window.tcp
        tab.editor.setPlainText('{"emoji":"🌍",\n "value": }')
        wait_for(lambda: tab.editor.diagnostic is not None)
        self.assertEqual(tab.problems.count(), 1)
        self.assertEqual(tab.editor.diagnostic.line, 2)
        self.assertEqual(len(tab.editor.extraSelections()), 1)
        original = tab.editor.toPlainText()
        tab.format_button.click()
        self.assertEqual(tab.editor.toPlainText(), original)
        tab.problems.itemClicked.emit(tab.problems.item(0))
        self.assertEqual(tab.editor.textCursor().blockNumber(), 1)
        tab.editor.setPlainText('{"a":')
        tab.editor.validate()
        tab.editor.jump_to_error()
        self.assertEqual(tab.editor.textCursor().position(), len('{"a":'))
        APP.processEvents()  # Exercise EOF marker paint.
        tab.editor.setPlainText('{"value":1e999,"same":-0.000,"same":true}')
        tab.format_button.click()
        self.assertIsNone(tab.editor.diagnostic)
        self.assertEqual(tab.problems.count(), 0)
        self.assertIn('1e999', tab.editor.toPlainText())
        self.assertIn('-0.000', tab.editor.toPlainText())
        tab.editor.undo()
        self.assertNotIn('\n', tab.editor.toPlainText())
        tab.reveal_panel(1)
        tab.toggle_panel()
        self.assertTrue(tab.panel.isHidden())
        self.window.output_button.click()
        self.assertFalse(tab.panel.isHidden())
        tab.output.clear()
        self.assertEqual(tab.output.toPlainText(), '')
        # A wide Unicode character before the diagnostic uses UTF-16 cursor positions.
        tab.editor.setPlainText('{"x":"🌍","bad":}')
        tab.editor.validate()
        tab.editor.jump_to_error()
        self.assertEqual(tab.editor.textCursor().position(), tab.editor.diagnostic.position + 1)
        self.window.tabs.setCurrentIndex(1)
        self.window.tabs.setCurrentIndex(0)
        self.assertEqual(tab.editor.toPlainText(), '{"x":"🌍","bad":}')
        palette = tab.editor.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor('#202124'))
        tab.editor.setPalette(palette)
        APP.processEvents()
        self.assertTrue(tab.editor.document().firstBlock().layout().formats())
        tab.output.setPlainText('\n'.join(str(i) for i in range(600)))
        self.assertEqual(tab.output.blockCount(), 500)

    def test_tcp_persistent_exact_framing_and_rejection(self):
        server = self.tcp_server()
        tab = self.window.tcp
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected' and server.hasPendingConnections())
        peer = server.nextPendingConnection()
        self.assertFalse(tab.host.isEnabled())
        payload = '{ "message": "Ħello 🌍" }\n'
        tab.editor.setPlainText(payload)
        data = bytearray()
        peer.readyRead.connect(lambda: data.extend(bytes(peer.readAll())))
        for _ in range(2):
            previous = len(data)
            tab.send_button.click()
            wait_for(lambda: len(data) >= previous + 4 + len(payload.encode()))
            self.assertEqual(struct.unpack('>I', data[previous:previous + 4])[0], len(payload.encode()))
            self.assertEqual(data[previous + 4:], payload.encode())
            wait_for(lambda: not tab.transport.busy)
        self.assertFalse(server.hasPendingConnections())
        previous = len(data)
        tab.editor.setPlainText('{"invalid":}')
        tab.send_button.click()
        APP.processEvents()
        self.assertEqual(len(data), previous)
        tab.editor.setPlainText('"too large"')
        # Endpoint/limit controls are locked while connected; transport also guards size.
        tab.maximum.setText('2')
        tab.send()
        self.assertIn('limit is 2 bytes', tab.output.toPlainText())
        self.assertEqual(len(data), previous)
        peer.abort()
        wait_for(lambda: tab.transport.state == 'disconnected')
        self.assertTrue(tab.host.isEnabled())
        server.close()

    def test_tcp_one_shot_failure_timeout_and_cancel(self):
        server = self.tcp_server()
        tab = self.window.tcp
        tab.persistent.setChecked(False)
        self.assertFalse(tab.connect_button.isEnabled())
        self.assertTrue(tab.send_button.isEnabled())
        tab.send_button.click()
        wait_for(server.hasPendingConnections)
        peer = server.nextPendingConnection()
        wait_for(lambda: peer.bytesAvailable() > 0)
        frame = bytes(peer.readAll())
        self.assertEqual(frame[4:], tab.editor.toPlainText().encode())
        wait_for(lambda: tab.transport.state == 'disconnected')
        server.close()
        tab.port.setValue(available_port())
        tab.send_button.click()
        wait_for(lambda: tab.transport.state == 'disconnected')
        self.assertIn('ERROR:', tab.output.toPlainText())
        tab.persistent.setChecked(True)
        tab.connect_button.click()
        tab.disconnect_button.click()
        self.assertEqual(tab.transport.state, 'disconnected')
        # Deterministic timeout handler, avoiding dependence on router behaviour.
        tab.transport._timeout()
        self.assertIn('timed out', tab.output.toPlainText())
        self.assertIsNone(tab.transport.pending)

    def test_mqtt_wire_publish_and_independent_connections(self):
        broker = WireBroker()
        subscriber = socket.create_connection(('127.0.0.1', broker.port), timeout=2)
        try:
            # Independent subscriber, with a minimal MQTT 3.1.1 CONNECT.
            subscriber.sendall(packet(0x10, b'\x00\x04MQTT\x04\x02\x00\x1e\x00\x04test'))
            self.assertEqual(receive(subscriber), (0x20, b'\x00\x00'))
            topic = b'liminal/test'
            subscriber.sendall(packet(0x82, b'\x00\x01' + struct.pack('>H', len(topic)) + topic + b'\x00'))
            self.assertEqual(receive(subscriber)[0], 0x90)
            tab = self.window.mqtt
            tab.host.setText('127.0.0.1')
            tab.port.setValue(broker.port)
            tab.topic.setText(topic.decode())
            tab.username.setText('tester')
            tab.password.setText('test-password')
            tab.connect_button.click()
            wait_for(lambda: tab.transport.state == 'connected')
            self.assertIn('code 0', tab.output.toPlainText())
            self.assertFalse(tab.password.isEnabled())
            server = self.tcp_server()
            self.window.tcp.connect_button.click()
            wait_for(lambda: self.window.tcp.transport.state == 'connected')
            self.window.tabs.setCurrentIndex(1)
            payload = '{"message":"Ħello 🌍"}'
            tab.editor.setPlainText(payload)
            for count in [1, 2]:
                tab.send_button.click()
                wait_for(lambda: len(broker.messages) == count and not tab.transport.busy)
                header, body = receive(subscriber)
                self.assertEqual(header, 0x30)
                self.assertEqual(body[2 + len(topic):], payload.encode())
                self.assertEqual(broker.messages[-1], (topic.decode(), payload.encode(), 0, False))
            self.assertEqual(len(broker.connects), 2)  # subscriber + one persistent sender
            tab.topic.setText('bad/#')
            tab.send()
            self.assertIn('without +, #', tab.output.toPlainText())
            self.assertEqual(len(broker.messages), 2)
            self.assertEqual(self.window.tcp.transport.state, 'connected')
            broker.close_clients()
            wait_for(lambda: tab.transport.state == 'disconnected')
            self.assertTrue(tab.username.isEnabled())
            server.close()
        finally:
            subscriber.close()
            broker.close()

    def test_mqtt_rejection_timeout_refusal_and_close(self):
        tab = self.window.mqtt
        broker = WireBroker(reject=5)
        try:
            tab.host.setText('127.0.0.1')
            tab.port.setValue(broker.port)
            tab.connect_button.click()
            wait_for(lambda: tab.transport.state == 'disconnected' and not tab.transport.is_running())
            self.assertIn('rejected', tab.output.toPlainText())
            self.assertFalse(tab.send_button.isEnabled())
        finally:
            broker.close()
        broker = WireBroker(acknowledge=False)
        try:
            tab.transport.timeout_seconds = 0.3
            tab.port.setValue(broker.port)
            tab.connect_button.click()
            wait_for(lambda: tab.transport.state == 'disconnected' and not tab.transport.is_running())
            self.assertIn('timed out', tab.output.toPlainText())
        finally:
            broker.close()
        tab.port.setValue(available_port())
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'disconnected' and not tab.transport.is_running())
        self.assertIn('No automatic retry', tab.output.toPlainText())
        broker = WireBroker()
        try:
            tab.port.setValue(broker.port)
            tab.connect_button.click()
            wait_for(lambda: tab.transport.state == 'connected')
            self.window.close()
            wait_for(lambda: not tab.transport.is_running() and not self.window.isVisible())
        finally:
            broker.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
