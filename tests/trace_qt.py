"""Trace/UI integration checks with real local TCP and MQTT peers."""
from pathlib import Path
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QMessageBox
from liminal_desk.window import MainWindow
from liminal_desk.receiving import ReceivedMessage
from smoke_qt import APP, available_port, wait_for
from wire_broker import WireBroker


def trace(first='{"event":"first"}', delay=100, second='{"event":"second"}'):
    return '{"version":1,"steps":[{"type":"send","payload":' + first + '},{"type":"delay","duration_ms":' + str(delay) + '},{"type":"send","payload":' + second + '}]}'


class TraceUiTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow(preferences_path=False)
        self.window.show()
        APP.processEvents()
        self.servers, self.brokers = [], []

    def tearDown(self):
        for tab in (self.window.tcp, self.window.mqtt):
            tab.trace_saved_text = tab.trace_editor.toPlainText()
            tab.shutdown()
        wait_for(lambda: not self.window.mqtt.transport.is_running(), 7)
        for broker in self.brokers:
            broker.close()
        for server in self.servers:
            server.close()
        self.window.close()
        APP.processEvents()

    def tcp(self):
        server = QTcpServer()
        self.assertTrue(server.listen(QHostAddress.SpecialAddress.LocalHost, 0))
        self.servers.append(server)
        tab = self.window.tcp
        tab.port.setValue(server.serverPort())
        tab.connect_button.click()
        wait_for(lambda: server.hasPendingConnections() and tab.transport.state == 'connected')
        peer = server.nextPendingConnection()
        self.frames, self.arrivals, self.buffer = [], [], bytearray()
        def read():
            self.buffer.extend(bytes(peer.readAll()))
            while len(self.buffer) >= 4:
                size = struct.unpack('>I', self.buffer[:4])[0]
                if len(self.buffer) < size + 4:
                    break
                self.frames.append(bytes(self.buffer[4:size + 4]))
                self.arrivals.append(time.monotonic_ns())
                del self.buffer[:size + 4]
        peer.readyRead.connect(read)
        return tab, peer

    def mqtt(self):
        broker = WireBroker()
        self.brokers.append(broker)
        tab = self.window.mqtt
        tab.port.setValue(broker.port)
        tab.topic.setText('factory/trace')
        tab.subscribe_filter.setText('factory/#')
        tab.connect_button.click()
        wait_for(lambda: tab.transport.state == 'connected')
        tab.subscribe_button.click()
        wait_for(lambda: tab.transport.subscription_state == 'active')
        return tab, broker

    def set_trace(self, tab, source):
        tab.mode.setCurrentIndex(1)
        tab.trace_editor.setPlainText(source)
        tab.trace_editor.validate()

    def test_tcp_trace_pause_resume_exact_frames_and_message_mode(self):
        tab, peer = self.tcp()
        self.assertTrue(tab.connection_fields.isHidden())
        self.assertIn('127.0.0.1', tab.summary.text())
        raw = '{"x":9007199254740993,"x":1e999,"text":"\\u0126🌍"}'
        self.set_trace(tab, trace(raw, 150, 'null'))
        tab.run_button.click()
        wait_for(lambda: tab.replay.state == 'waiting' and len(self.frames) == 1)
        self.assertTrue(tab.trace_editor.isReadOnly())
        self.assertFalse(tab.mode.isEnabled())
        tab.pause_button.click()
        QTest.qWait(100)
        self.assertEqual(tab.replay.state, 'paused')
        self.assertEqual(self.frames, [raw.encode()])
        tab.pause_button.click()
        wait_for(lambda: tab.replay.state == 'completed' and len(self.frames) == 2)
        self.assertEqual(self.frames, [raw.encode(), b'null'])
        self.assertGreaterEqual(self.arrivals[1] - self.arrivals[0], 200000000)
        self.assertFalse(tab.trace_editor.isReadOnly())
        tab.mode.setCurrentIndex(0)
        ordinary = '[{"type":"delay","duration_ms":200}]'
        tab.editor.setPlainText(ordinary)
        tab.send_button.click()
        wait_for(lambda: len(self.frames) == 3)
        self.assertEqual(self.frames[-1], ordinary.encode())
        tab.mode.setCurrentIndex(1)
        self.assertEqual(tab.trace_editor.toPlainText(), trace(raw, 150, 'null'))

    def test_full_validation_before_transmission_and_connection_loss_paused(self):
        tab, peer = self.tcp()
        tab.maximum.setText('2')
        self.set_trace(tab, trace('{}', 0, '"oversized"'))
        tab.run_trace()
        QTest.qWait(40)
        self.assertEqual(self.frames, [])
        self.assertIn('TRACE_INVALID', tab.problems.item(0).text())
        tab.maximum.setText('1048576')
        self.set_trace(tab, '{"version":1,"steps":[{"type":"send"}]}')
        self.assertFalse(tab.run_button.isEnabled())
        tab.run_trace()
        self.assertEqual(self.frames, [])
        self.set_trace(tab, trace('{}', 1000, 'null'))
        tab.run_button.click()
        wait_for(lambda: tab.replay.state == 'waiting')
        tab.pause_button.click()
        peer.abort()
        wait_for(lambda: tab.replay.state == 'error')
        QTest.qWait(100)
        self.assertEqual(self.frames, [b'{}'])
        self.assertFalse(tab.replay.timer.isActive())
        self.assertFalse(tab.panel.isVisible())
        self.assertGreater(tab.problems.count(), 0)

    def test_simultaneous_tcp_mqtt_traces_receiving_and_stop(self):
        tcp, peer = self.tcp()
        mqtt, broker = self.mqtt()
        self.set_trace(tcp, trace(delay=500))
        self.set_trace(mqtt, trace(delay=60))
        tcp.run_button.click()
        mqtt.run_button.click()
        wait_for(lambda: tcp.replay.state == 'waiting')
        tcp.pause_button.click()
        self.window.tabs.setCurrentIndex(1)
        self.assertFalse(mqtt.topic.isEnabled())
        mqtt.send()  # Manual send must not interleave with the trace.
        wait_for(lambda: mqtt.replay.state == 'completed' and mqtt.viewer.model.rowCount() == 2)
        self.assertEqual([item[1] for item in broker.messages], [b'{"event":"first"}', b'{"event":"second"}'])
        self.assertEqual(len(broker.connects), 1)
        self.assertEqual(tcp.replay.state, 'paused')
        tcp.stop_button.click()
        self.assertEqual(tcp.replay.state, 'stopped')
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(tcp.transport.state, 'connected')
        self.assertEqual(mqtt.transport.subscription_state, 'active')

    def test_status_errors_panel_controls_and_follow_latest(self):
        tcp = self.window.tcp
        tcp.listen_port.setValue(available_port())
        tcp.start_listener_button.click()
        tcp.port.setValue(available_port())
        tcp.connect_button.click()
        wait_for(lambda: 'sender' in tcp.errors)
        self.assertIn('TCP_CONNECT_REFUSED', self.window.primary_status.text())
        self.assertIn('Waiting', self.window.secondary_status.text())
        self.assertIn(self.window._colours()['green'], self.window.secondary_status.styleSheet())
        self.assertTrue(tcp.panel.isHidden())
        self.assertFalse(self.window.tabs.tabIcon(0).isNull())
        tcp.editor.setPlainText('{bad}')
        tcp.editor.validate()
        tcp.editor.setPlainText('{}')
        tcp.editor.validate()
        self.assertIn('sender', tcp.errors)
        self.window.problems_button.click()
        self.assertFalse(tcp.panel.isHidden())
        self.assertEqual(tcp.panel_tabs.currentIndex(), 0)
        self.window.output_button.click()
        self.assertEqual(tcp.panel_tabs.currentIndex(), 1)
        self.window.output_button.click()
        self.assertTrue(tcp.panel.isHidden())
        viewer = tcp.viewer
        viewer.add_message(ReceivedMessage(b'1', 'peer'))
        viewer.follow.setChecked(True)
        viewer.add_message(ReceivedMessage(b'2', 'peer'))
        self.assertEqual(viewer.selected.raw, b'2')
        viewer.table.selectRow(0)
        self.assertFalse(viewer.follow.isChecked())
        viewer.add_message(ReceivedMessage(b'3', 'peer'))
        self.assertEqual(viewer.selected.raw, b'1')
        self.window.tabs.setCurrentIndex(1)
        self.assertIn('Broker', self.window.primary_status.text())
        self.assertIn('Subscription', self.window.secondary_status.text())
        self.assertIn('sender', tcp.errors)

    def test_trace_files_drafts_failures_and_unsaved_close(self):
        tab = self.window.tcp
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'draft.json'
            self.set_trace(tab, '{"invalid":')
            with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(path), '')):
                self.assertTrue(tab.save_trace())
            self.assertEqual(path.read_text(), '{"invalid":')
            self.assertFalse(tab.trace_dirty)
            self.assertIsNotNone(tab.trace_editor.diagnostic)
            self.assertIn('invalid draft', tab.output.toPlainText())
            original = tab.trace_editor.toPlainText()
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(Path(directory) / 'missing.json'), '')):
                self.assertFalse(tab.open_trace())
            self.assertEqual(tab.trace_editor.toPlainText(), original)
            path.write_text(trace())
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                self.assertTrue(tab.open_trace())
            self.assertEqual(tab.trace_editor.toPlainText(), trace())
            tab.trace_editor.setPlainText(trace(delay=999))
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
                self.window.close()
                self.assertTrue(self.window.isVisible())
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Discard):
                self.assertTrue(tab.confirm_trace_replace())
            with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(Path(directory) / 'missing' / 'file.json'), '')):
                self.assertFalse(tab.save_trace())
            self.assertTrue(tab.trace_dirty)
            self.assertEqual(tab.trace_editor.toPlainText(), trace(delay=999))

    def test_close_during_replay_cancels_pending_steps(self):
        tab, peer = self.tcp()
        self.set_trace(tab, trace(delay=1000))
        tab.run_button.click()
        wait_for(lambda: tab.replay.state == 'waiting' and len(self.frames) == 1)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        wait_for(lambda: not self.window.isVisible())
        self.assertFalse(tab.replay.active)
        self.assertFalse(tab.replay.timer.isActive())
        self.assertFalse(tab.replay.advance.isActive())
        self.assertEqual(tab.transport.state, 'disconnected')
        self.assertEqual(len(self.frames), 1)

    def test_layout_preferences_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'layout.json'
            self.window.preferences_path = path
            self.window.tcp.reveal_panel(1)
            self.window.tcp.splitter.setSizes([420, 210])
            self.window.tcp.toggle_panel()
            self.assertGreater(self.window.tcp._panel_sizes[1], 0)
            self.window.tcp.reveal_panel(1)
            self.window.mqtt.host.setText('private.example')
            self.window.mqtt.password.setText('secret-not-saved')
            self.window.save_preferences()
            text = path.read_text()
            self.assertNotIn('private.example', text)
            self.assertNotIn('secret-not-saved', text)
            restored = MainWindow(preferences_path=path)
            restored.show()
            APP.processEvents()
            try:
                self.assertFalse(restored.tcp.panel.isHidden())
                self.assertEqual(restored.tcp.panel_tabs.currentIndex(), 1)
                self.assertEqual(restored.mqtt.host.text(), '127.0.0.1')
                self.assertEqual(restored.mqtt.password.text(), '')
            finally:
                restored.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
