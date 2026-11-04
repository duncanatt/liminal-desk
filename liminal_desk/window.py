"""Native send/receive workspace, status bar, diagnostics and trace files."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTabWidget,
    QVBoxLayout, QWidget)

from .controls import Disclosure, TOOL_STYLE, tool
from .editor import JsonEditor
from .message_view import MessageViewer
from .receiving import TcpListener
from .samples import MQTT_SAMPLE, trace_sample
from .trace import Replay, TraceEditor, TraceError, parse_trace
from .transports import MqttTransport, TcpTransport
from .validation import endpoint, json_bytes, tcp_frame, topic_name


class TopicDropdown(QComboBox):
    def __init__(self, suggestions, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(suggestions)
        self.lineEdit().setMaxLength(65535)

    def text(self):
        return self.currentText()

    def setText(self, value):
        self.setEditText(value)

    def setPlaceholderText(self, value):
        self.lineEdit().setPlaceholderText(value)


def port(value, name):
    field = QSpinBox()
    field.setRange(1, 65535)
    field.setValue(value)
    field.setAccessibleName(name)
    return field


class ConnectionTab(QWidget):
    changed = Signal()

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.ready = False
        self.errors = {}
        self.transport = TcpTransport(self) if kind == 'TCP' else MqttTransport(self)
        self.replay = Replay(self.transport, self)
        self.trace_path = None
        self.trace_saved_text = ''
        self.host = QLineEdit('127.0.0.1')
        self.host.setAccessibleName(f'{kind} host')
        self.port = port(9999 if kind == 'TCP' else 1883, f'{kind} port')
        self.maximum = QLineEdit('1048576')
        self.receive_maximum = QLineEdit('1048576')
        self.maximum.setAccessibleName('Maximum TCP payload bytes')
        self.receive_maximum.setAccessibleName(f'{kind} receive byte limit')
        self.connect_button = QPushButton('Connect')
        self.disconnect_button = QPushButton('Disconnect')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(8)
        layout.addWidget(self._connection_controls())
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setHandleWidth(6)
        self.workspace = QSplitter(Qt.Orientation.Horizontal)
        self.workspace.addWidget(self._sender())
        self.receive_area = self._receiver()
        self.workspace.addWidget(self.receive_area)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.setSizes([520, 520])
        self.splitter.addWidget(self.workspace)
        self.panel = self._diagnostics()
        self.splitter.addWidget(self.panel)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizes([620, 140])
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)
        self._panel_sizes = [620, 140]
        self.panel.hide()
        self.splitter.splitterMoved.connect(self._remember_panel_sizes)
        self.connect_button.clicked.connect(self.connect_to)
        self.disconnect_button.clicked.connect(self.disconnect)
        self.transport.activity.connect(lambda msg: self.log(msg, 'sender' if kind == 'TCP' else 'broker'))
        self.transport.state_changed.connect(self._transport_state)
        self.transport.sent.connect(self.refresh)
        self.replay.changed.connect(self.refresh)
        self.replay.activity.connect(lambda msg: self.log(msg))
        self.replay.failed.connect(lambda msg: self.problem('trace', 'TRACE_SEND_FAILED', msg))
        for editor in (self.editor, self.trace_editor):
            editor.diagnostic_changed.connect(self.show_diagnostic)
            editor.byte_count_changed.connect(self.update_bytes)
        self.trace_editor.textChanged.connect(self._trace_edited)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.ready = True
        self._mode_changed()
        self.refresh()
        self.log('Ready. Local transmission does not confirm Liminal processing.')

    def _connection_controls(self):
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.connection_fields = QWidget()
        fields = QHBoxLayout(self.connection_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.addWidget(QLabel('Server' if self.kind == 'TCP' else 'Broker'))
        fields.addWidget(self.host, 1)
        fields.addWidget(QLabel('Port'))
        fields.addWidget(self.port)
        self.summary = QLabel()
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.connection_fields, 1)
        row.addWidget(self.summary, 1)
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        layout.addLayout(row)
        self.connection_settings = Disclosure('Connection settings')
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.connection_settings.body.addLayout(form)
        self.locked_fields = [self.host, self.port]
        if self.kind == 'TCP':
            self.persistent = QCheckBox('Keep connection open')
            self.persistent.setChecked(True)
            form.addRow(self.persistent)
            form.addRow('Max sent payload (bytes)', self.maximum)
            self.locked_fields += [self.persistent, self.maximum]
            self.persistent.toggled.connect(self.refresh)
        else:
            self.username = QLineEdit()
            self.username.setPlaceholderText('Username (optional)')
            self.password = QLineEdit()
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password.setPlaceholderText('Password (not saved)')
            auth = QHBoxLayout()
            auth.addWidget(self.username)
            auth.addWidget(self.password)
            form.addRow('Authentication', auth)
            form.addRow('Max received payload (bytes)', self.receive_maximum)
            form.addRow(QLabel('MQTT 3.1.1 · publish QoS 0 · Retain off'))
            self.locked_fields += [self.username, self.password, self.receive_maximum]
        layout.addWidget(self.connection_settings)
        return outer

    def _sender(self):
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        title = QLabel('Send')
        title.setStyleSheet('font-weight:600;')
        self.mode = QComboBox()
        self.mode.addItems(['Message', 'Trace'])
        self.mode.setAccessibleName(f'{self.kind} sending mode')
        self.format_button = tool('Format JSON', 'Format JSON (⇧⌘F)')
        self.byte_count = QLabel()
        bar.addWidget(title)
        bar.addWidget(self.mode)
        bar.addStretch()
        bar.addWidget(self.byte_count)
        bar.addWidget(self.format_button)
        layout.addLayout(bar)
        if self.kind == 'MQTT':
            row = QHBoxLayout()
            self.topic = TopicDropdown(['liminal/sensors/esp32-001/imu'])
            self.topic.setAccessibleName('MQTT publish topic')
            row.addWidget(QLabel('Publish topic'))
            row.addWidget(self.topic, 1)
            layout.addLayout(row)
        self.trace_file_bar = QWidget()
        files = QHBoxLayout(self.trace_file_bar)
        files.setContentsMargins(0, 0, 0, 0)
        self.trace_name = QLabel('Untitled trace')
        self.trace_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.open_trace_button = tool('Open', 'Open Trace')
        self.save_trace_button = tool('Save', 'Save Trace')
        files.addWidget(self.trace_name, 1)
        files.addWidget(self.open_trace_button)
        files.addWidget(self.save_trace_button)
        layout.addWidget(self.trace_file_bar)
        self.editor = JsonEditor()
        self.editor.setPlainText('{"test": "data", "value": 42}' if self.kind == 'TCP' else MQTT_SAMPLE)
        self.editor.validate()
        self.editor.setAccessibleName(f'{self.kind} JSON payload')
        self.trace_editor = TraceEditor()
        self.trace_editor.setPlainText(trace_sample(self.editor.toPlainText()))
        self.trace_editor.validate()
        self.trace_editor.setAccessibleName(f'{self.kind} JSON trace')
        self.trace_saved_text = self.trace_editor.toPlainText()
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(self.editor)
        self.editor_stack.addWidget(self.trace_editor)
        layout.addWidget(self.editor_stack, 1)
        footer = QHBoxLayout()
        self.validation_status = QLabel()
        self.validation_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.validation_status, 1)
        self.send_button = QPushButton('Send' if self.kind == 'TCP' else 'Publish')
        self.run_button = QPushButton('Run')
        self.run_button.setToolTip('Run a valid trace on the connected sender. TCP requires Keep connection open.')
        self.pause_button = QPushButton('Pause')
        self.stop_button = QPushButton('Stop')
        self.stop_button.setToolTip('Cancel remaining steps; a send already in flight cannot be retracted.')
        for button in (self.send_button, self.run_button, self.pause_button, self.stop_button):
            footer.addWidget(button)
        layout.addLayout(footer)
        self.format_button.clicked.connect(self.format_current)
        self.send_button.clicked.connect(self.send)
        self.run_button.clicked.connect(self.run_trace)
        self.pause_button.clicked.connect(lambda: self.replay.resume() if self.replay.state == 'paused' else self.replay.pause())
        self.stop_button.clicked.connect(self.replay.stop)
        self.open_trace_button.clicked.connect(self.open_trace)
        self.save_trace_button.clicked.connect(self.save_trace)
        return area

    def _receiver(self):
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel('Receive')
        title.setStyleSheet('font-weight:600;')
        header.addWidget(title)
        header.addStretch()
        self.receive_status = QLabel()
        if self.kind == 'TCP':
            self.listener = TcpListener(self)
            self.start_listener_button = QPushButton('Start Listening')
            self.stop_listener_button = QPushButton('Stop Listening')
            header.addWidget(self.start_listener_button)
            header.addWidget(self.stop_listener_button)
            layout.addLayout(header)
            self.listen_fields = QWidget()
            row = QHBoxLayout(self.listen_fields)
            row.setContentsMargins(0, 0, 0, 0)
            self.listen_host = QLineEdit('127.0.0.1')
            self.listen_host.setAccessibleName('TCP listener bind address')
            self.listen_port = port(10000, 'TCP listener port')
            row.addWidget(QLabel('Bind'))
            row.addWidget(self.listen_host, 1)
            row.addWidget(QLabel('Port'))
            row.addWidget(self.listen_port)
            layout.addWidget(self.listen_fields)
            layout.addWidget(self.receive_status)
            self.receive_settings = Disclosure('Receive settings')
            form = QFormLayout()
            form.addRow('Max received payload (bytes)', self.receive_maximum)
            self.receive_settings.body.addLayout(form)
            self.receive_settings.body.addWidget(QLabel('History: 500 messages / 10 MiB · oldest removed first'))
            layout.addWidget(self.receive_settings)
            self.start_listener_button.clicked.connect(self.start_listener)
            self.stop_listener_button.clicked.connect(self.listener.stop)
            self.listener.activity.connect(lambda msg: self.log(msg, 'receive'))
            self.listener.state_changed.connect(self._listener_state)
        else:
            layout.addLayout(header)
            row = QHBoxLayout()
            self.subscribe_filter = TopicDropdown(['liminal/#', 'liminal/sensors/esp32-001/imu'])
            self.subscribe_filter.setAccessibleName('MQTT subscribe filter')
            self.subscribe_button = QPushButton('Subscribe')
            self.unsubscribe_button = QPushButton('Unsubscribe')
            row.addWidget(self.subscribe_filter, 1)
            row.addWidget(self.subscribe_button)
            row.addWidget(self.unsubscribe_button)
            layout.addLayout(row)
            self.subscribe_filter.setToolTip('Subscription filter · + matches a level; # matches remaining levels')
            self.subscribe_button.clicked.connect(self.subscribe)
            self.unsubscribe_button.clicked.connect(self.unsubscribe)
            self.transport.subscription_changed.connect(self._subscription_state)
        self.viewer = MessageViewer()
        self.viewer.limits.hide()
        self.viewer.activity.connect(lambda msg: self.log(msg, 'receive'))
        source = self.listener if self.kind == 'TCP' else self.transport
        source.message_received.connect(self.viewer.add_message)
        layout.addWidget(self.viewer, 1)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(area)
        scroll.setMinimumWidth(390)
        return scroll

    def _diagnostics(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        bar = QHBoxLayout()
        bar.addWidget(QLabel('Diagnostics'))
        bar.addStretch()
        copy = tool('Copy', 'Copy Output')
        clear = tool('Clear', 'Clear Output')
        self.dismiss_button = tool('Dismiss', 'Dismiss selected operation error')
        close = tool('×', 'Close diagnostic panel')
        for button in (copy, clear, self.dismiss_button, close):
            bar.addWidget(button)
        layout.addLayout(bar)
        self.panel_tabs = QTabWidget()
        self.problems = QListWidget()
        self.problems.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(500)
        self.output.setFont(self.editor.font())
        for widget in (self.problems, self.output):
            widget.setMinimumHeight(35)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.panel_tabs.addTab(self.problems, 'Problems')
        self.panel_tabs.addTab(self.output, 'Output')
        layout.addWidget(self.panel_tabs)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.output.toPlainText()))
        clear.clicked.connect(self.output.clear)
        close.clicked.connect(self.toggle_panel)
        self.dismiss_button.clicked.connect(self.dismiss_problem)
        self.problems.itemClicked.connect(self.navigate_problem)
        self.problems.itemActivated.connect(self.navigate_problem)
        return panel

    @property
    def current_editor(self):
        return self.trace_editor if self.mode.currentIndex() else self.editor

    @property
    def trace_dirty(self):
        return self.trace_editor.toPlainText() != self.trace_saved_text

    def _trace_edited(self):
        name = self.trace_path.name if self.trace_path else 'Untitled trace'
        self.trace_name.setText(name + (' •' if self.trace_dirty else ''))
        self.trace_name.setToolTip(str(self.trace_path) if self.trace_path else name)

    def _mode_changed(self, *_):
        if not self.ready:
            return
        trace = self.mode.currentIndex() == 1
        self.editor_stack.setCurrentIndex(int(trace))
        self.trace_file_bar.setVisible(trace)
        self.send_button.setVisible(not trace)
        for button in (self.run_button, self.pause_button, self.stop_button):
            button.setVisible(trace)
        self.current_editor.validate()
        self._trace_edited()
        self.refresh()

    def format_current(self):
        if not self.replay.active:
            self.current_editor.format_json()

    def update_bytes(self, *_):
        if self.ready:
            self.byte_count.setText(f'{len(self.current_editor.toPlainText().encode("utf-8", errors="replace")):,} bytes')

    def show_diagnostic(self, *_):
        if self.ready:
            self.rebuild_problems()
            self.refresh()

    def problem(self, scope, code, message):
        self.errors[scope] = (code, message)
        if self.ready:
            self.rebuild_problems()
            self.changed.emit()

    def clear_problem(self, scope):
        self.errors.pop(scope, None)
        if self.ready:
            self.rebuild_problems()
            self.changed.emit()

    def rebuild_problems(self):
        self.problems.clear()
        for mode, editor in enumerate((self.editor, self.trace_editor)):
            if editor.diagnostic:
                code = 'TRACE_INVALID' if mode else 'JSON_INVALID'
                item = QListWidgetItem(f'{code} · {editor.error_text()}')
                item.setData(Qt.ItemDataRole.UserRole, ('editor', mode))
                self.problems.addItem(item)
        for scope, (code, detail) in self.errors.items():
            item = QListWidgetItem(f'{code} · {detail}')
            item.setToolTip(detail)
            item.setData(Qt.ItemDataRole.UserRole, ('operation', scope))
            self.problems.addItem(item)
        self.panel_tabs.setTabText(0, f'Problems ({self.problems.count()})' if self.problems.count() else 'Problems')

    def navigate_problem(self, item):
        kind, target = item.data(Qt.ItemDataRole.UserRole)
        if kind == 'editor':
            if not self.replay.active:
                self.mode.setCurrentIndex(target)
            editor = self.trace_editor if target else self.editor
            editor.jump_to_error()
        else:
            self.show_controls(target)

    def dismiss_problem(self):
        item = self.problems.currentItem()
        if item:
            kind, target = item.data(Qt.ItemDataRole.UserRole)
            if kind == 'operation':
                self.clear_problem(target)

    def _remember_panel_sizes(self, *_):
        if not self.panel.isHidden():
            self._panel_sizes = self.splitter.sizes()

    def toggle_panel(self):
        if self.panel.isHidden():
            self.panel.show()
            self.splitter.setSizes(self._panel_sizes)
        else:
            self._remember_panel_sizes()
            self.panel.hide()
        self.changed.emit()

    def reveal_panel(self, index):
        if self.panel.isHidden():
            self.panel.show()
            self.splitter.setSizes(self._panel_sizes)
        self.panel_tabs.setCurrentIndex(index)
        self.changed.emit()

    def show_controls(self, scope):
        if scope in self.errors:
            self.reveal_panel(0)
        if scope in ('listener', 'receive') and self.kind == 'TCP':
            self.receive_settings.reveal()
        elif scope in ('sender', 'broker', 'settings'):
            self.connection_settings.reveal()

    def log(self, message, scope=None):
        self.output.appendPlainText(f'{datetime.now():%H:%M:%S}  {message}')
        if message.startswith('ERROR:') and scope:
            lower = message.lower()
            if scope == 'broker' and ('subscription' in lower or 'unsubscribe' in lower):
                scope = 'subscription'
            code = {'sender': 'TCP_SEND_FAILED', 'broker': 'MQTT_CONNECTION_FAILED',
                    'receive': 'RECEIVE_ERROR', 'subscription': 'MQTT_SUB_REJECTED'}.get(scope, 'OPERATION_FAILED')
            if 'refused' in lower and self.kind == 'TCP':
                code = 'TCP_CONNECT_REFUSED'
            if 'frame declares' in lower:
                code = 'TCP_FRAME_TOO_LARGE'
            if scope == 'sender':
                error = self.transport.socket.error()
                message += f' [Qt socket code {error.value}: {error.name}]'
            self.problem(scope, code, message.removeprefix('ERROR: '))
        self.changed.emit()

    def _transport_state(self, state):
        if state in ('connecting', 'connected'):
            self.clear_problem('sender' if self.kind == 'TCP' else 'broker')
        self.refresh()

    def _listener_state(self, state):
        if state == 'listening':
            self.clear_problem('listener')
        self.refresh()

    def _subscription_state(self, state):
        if state in ('subscribing', 'active'):
            self.clear_problem('subscription')
        self.refresh()

    def refresh(self, *_):
        if not self.ready:
            return
        state = self.transport.state
        active = state != 'disconnected'
        replay = self.replay.active
        for field in self.locked_fields:
            field.setEnabled(not active and not replay)
        self.connection_fields.setVisible(not active)
        self.summary.setVisible(active)
        self.summary.setText(f'{"Server" if self.kind == "TCP" else "Broker"} · {self.host.text()}:{self.port.value()} · {state.capitalize()}')
        self.connect_button.setVisible(not active)
        self.disconnect_button.setVisible(active)
        persistent = self.persistent.isChecked() if self.kind == 'TCP' else True
        self.connect_button.setEnabled(not active and persistent and not replay)
        self.disconnect_button.setEnabled(active and state != 'disconnecting')
        can_send = (state == 'connected' if persistent else state == 'disconnected') and not self.transport.busy
        self.send_button.setEnabled(can_send and not replay)
        self.run_button.setEnabled(state == 'connected' and not self.transport.busy and not replay and persistent and self.trace_editor.diagnostic is None)
        self.mode.setEnabled(not replay)
        self.trace_editor.setReadOnly(replay)
        self.open_trace_button.setEnabled(not replay)
        self.save_trace_button.setEnabled(not replay)
        self.format_button.setEnabled(not replay)
        self.pause_button.setText('Resume' if self.replay.state == 'paused' else 'Pause')
        self.pause_button.setEnabled(self.replay.state in ('running', 'waiting', 'paused'))
        self.stop_button.setEnabled(replay and self.replay.state != 'stopping')
        if self.kind == 'MQTT':
            self.topic.setEnabled(not replay)
        error = self.current_editor.diagnostic
        if replay:
            self.validation_status.setText(self.replay.progress)
        elif error:
            self.validation_status.setText('⨯ Trace error' if self.mode.currentIndex() else '⨯ JSON error')
        else:
            self.validation_status.setText('Checking…' if self.current_editor.timer.isActive() else ('✓ Valid trace' if self.mode.currentIndex() else '✓ Valid JSON'))
        self.validation_status.setToolTip(self.replay.progress if replay else self.current_editor.error_text())
        self.validation_status.setStyleSheet('color:#d83b48;' if error else '')
        self.update_bytes()
        self.refresh_receiver()
        self.changed.emit()

    def refresh_receiver(self, *_):
        if not self.ready:
            return
        if self.kind == 'TCP':
            active = self.listener.state != 'stopped'
            for field in (self.listen_host, self.listen_port, self.receive_maximum):
                field.setEnabled(not active)
            self.listen_fields.setVisible(not active)
            self.start_listener_button.setVisible(not active)
            self.stop_listener_button.setVisible(active)
            self.receive_status.setVisible(active)
            self.receive_status.setText(f'Listening on {self.listen_host.text()}:{self.listen_port.value()}')
        else:
            state = self.transport.subscription_state
            connected = self.transport.state == 'connected'
            self.subscribe_filter.setEnabled(state in ('idle', 'rejected'))
            self.subscribe_button.setVisible(state in ('idle', 'rejected', 'subscribing'))
            self.subscribe_button.setEnabled(connected and state in ('idle', 'rejected'))
            self.unsubscribe_button.setVisible(state in ('active', 'unsubscribing'))
            self.unsubscribe_button.setEnabled(connected and state == 'active')

    def _receive_limit(self):
        try:
            value = int(self.receive_maximum.text())
        except ValueError:
            raise ValueError('Receive limit must be a whole number of bytes') from None
        if not 1 <= value <= 0xFFFFFFFF:
            raise ValueError('Receive limit must be between 1 and 4,294,967,295 bytes')
        return value

    def connect_to(self):
        self.clear_problem('sender' if self.kind == 'TCP' else 'broker')
        try:
            host, number = endpoint(self.host.text(), self.port.value())
            if self.kind == 'MQTT':
                self.transport.receive_maximum = self._receive_limit()
                if self.password.text() and not self.username.text():
                    raise ValueError('Enter a username when supplying a password')
                self.transport.connect_to(host, number, self.username.text(), self.password.text())
            else:
                self.transport.connect_to(host, number)
        except (ValueError, OSError) as exc:
            scope = 'sender' if self.kind == 'TCP' else 'broker'
            self.problem(scope, 'CONNECTION_INVALID', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def disconnect(self):
        self.replay.stop()
        self.transport.disconnect()

    def start_listener(self):
        self.clear_problem('listener')
        try:
            self.listener.start(self.listen_host.text(), self.listen_port.value(), self._receive_limit())
        except ValueError as exc:
            self.problem('listener', 'TCP_LISTEN_FAILED', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def subscribe(self):
        try:
            self.transport.subscribe(self.subscribe_filter.text())
        except ValueError as exc:
            self.problem('subscription', 'MQTT_FILTER_INVALID', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def unsubscribe(self):
        try:
            self.transport.unsubscribe()
        except ValueError as exc:
            self.problem('subscription', 'MQTT_UNSUBSCRIBE_FAILED', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def _send_settings(self):
        host, number = endpoint(self.host.text(), self.port.value())
        if self.kind == 'TCP':
            try:
                maximum = int(self.maximum.text())
            except ValueError:
                raise ValueError('Maximum payload must be a whole number of bytes.') from None
            tcp_frame(b'', maximum)
            return host, number, maximum
        return topic_name(self.topic.text()),

    def send(self):
        if self.replay.active:
            return
        if self.mode.currentIndex():
            self.run_trace()
            return
        if not self.editor.validate():
            self.editor.jump_to_error()
            return
        self.clear_problem('publish')
        try:
            payload = json_bytes(self.editor.toPlainText())
            settings = self._send_settings()
            if self.kind == 'TCP':
                self.transport.send(payload, settings[0], settings[1], self.persistent.isChecked(), settings[2])
            else:
                self.transport.publish(settings[0], payload)
        except (ValueError, OSError) as exc:
            self.problem('publish', 'SEND_INVALID', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def run_trace(self):
        if self.replay.active:
            return
        self.trace_editor.validate()
        if self.trace_editor.diagnostic:
            self.trace_editor.jump_to_error()
            return
        self.clear_problem('trace')
        try:
            steps = parse_trace(self.trace_editor.toPlainText())
            settings = self._send_settings()
            if self.kind == 'TCP':
                if not self.persistent.isChecked():
                    raise ValueError('Trace replay requires Keep connection open')
                for step in steps:
                    if step.kind == 'send':
                        tcp_frame(step.payload, settings[2])
                sender = lambda payload: self.transport.send(payload, settings[0], settings[1], True, settings[2])
            else:
                for step in steps:
                    if step.kind == 'send' and len(step.payload) + len(settings[0].encode()) + 2 > 268435455:
                        raise ValueError('A trace payload exceeds the MQTT packet limit')
                sender = lambda payload: self.transport.publish(settings[0], payload)
            self.replay.start(steps, sender)
        except (ValueError, OSError) as exc:
            self.problem('trace', 'TRACE_INVALID', str(exc))
            self.log('ERROR: ' + str(exc))
        self.refresh()

    def confirm_trace_replace(self):
        if not self.trace_dirty:
            return True
        answer = QMessageBox.question(self, 'Unsaved trace', f'Save changes to the {self.kind} trace?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_trace()
        return answer == QMessageBox.StandardButton.Discard

    def open_trace(self):
        if self.replay.active or not self.confirm_trace_replace():
            return False
        name, _ = QFileDialog.getOpenFileName(self, 'Open Trace', str(self.trace_path or ''), 'JSON traces (*.json);;All files (*)')
        if not name:
            return False
        try:
            text = Path(name).read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            self.problem('file', 'TRACE_READ_FAILED', str(exc))
            return False
        self.trace_path = Path(name)
        self.trace_saved_text = text
        self.trace_editor.setPlainText(text)
        self.mode.setCurrentIndex(1)
        self.trace_editor.validate()
        self._trace_edited()
        self.clear_problem('file')
        return True

    def save_trace(self):
        if self.replay.active:
            return False
        name, _ = QFileDialog.getSaveFileName(self, 'Save Trace', str(self.trace_path or 'trace.json'), 'JSON traces (*.json)')
        if not name:
            return False
        path = Path(name)
        temporary = None
        try:
            text = self.trace_editor.toPlainText()
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
            os.replace(temporary, path)
        except (OSError, UnicodeError) as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            self.problem('file', 'TRACE_WRITE_FAILED', str(exc))
            return False
        self.trace_path, self.trace_saved_text = path, text
        self.trace_editor.validate()
        self._trace_edited()
        self.clear_problem('file')
        self.log('Trace saved' + (' as an invalid draft.' if self.trace_editor.diagnostic else '.'))
        return True

    def shutdown(self):
        self.replay.shutdown()
        if self.kind == 'TCP':
            self.listener.shutdown()
        self.transport.shutdown()


class MainWindow(QMainWindow):
    def __init__(self, preferences_path=None):
        super().__init__()
        self.preferences_path = (Path(preferences_path) if preferences_path else
            Path(__file__).resolve().parents[1] / '.ui-preferences.json') if preferences_path is not False else None
        self.setWindowTitle('Liminal Desk')
        self.resize(1160, 850)
        self.setMinimumSize(950, 650)
        self.closing = False
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(16, 12, 16, 6)
        outer.setSpacing(8)
        title = QLabel('Liminal Desk')
        title.setStyleSheet('font-size:22px;font-weight:600;')
        outer.addWidget(title)
        self.tabs = QTabWidget()
        self.tcp, self.mqtt = ConnectionTab('TCP'), ConnectionTab('MQTT')
        self.tabs.addTab(self.tcp, 'TCP')
        self.tabs.addTab(self.mqtt, 'MQTT')
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(container)
        self.primary_status = tool('● Sender · Disconnected')
        self.secondary_status = tool('● Listener · Stopped')
        self.trace_status = tool('Trace · Ready')
        self.problems_button = tool('⨯ 0', 'Show Problems')
        self.output_button = tool('▤ Output', 'Show or hide Output (⌘J)')
        self.output_button.setCheckable(True)
        self.statusBar().setSizeGripEnabled(False)
        for button in (self.primary_status, self.secondary_status):
            self.statusBar().addWidget(button)
        for button in (self.trace_status, self.problems_button, self.output_button):
            self.statusBar().addPermanentWidget(button)
        self.primary_status.clicked.connect(lambda: self.tabs.currentWidget().show_controls('sender' if self.tabs.currentIndex() == 0 else 'broker'))
        self.secondary_status.clicked.connect(lambda: self.tabs.currentWidget().show_controls('listener' if self.tabs.currentIndex() == 0 else 'subscription'))
        self.trace_status.clicked.connect(self._trace_focus)
        self.problems_button.clicked.connect(lambda: self.tabs.currentWidget().reveal_panel(0))
        self.output_button.clicked.connect(self._toggle_output)
        for tab in (self.tcp, self.mqtt):
            tab.changed.connect(self.refresh_status)
            tab.panel_tabs.currentChanged.connect(self.refresh_status)
        self.tabs.currentChanged.connect(self.refresh_status)
        self._menus()
        self.close_timer = QTimer(self)
        self.close_timer.setInterval(50)
        self.close_timer.timeout.connect(self._finish_close)
        self.restore_preferences()
        self.refresh_status()

    def _colours(self):
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        return {'gray': '#a8acb3' if dark else '#636971', 'green': '#52c785' if dark else '#187c45',
                'amber': '#e5b354' if dark else '#976300', 'red': '#ff727e' if dark else '#bd2638'}

    def _indicator(self, button, text, colour, detail):
        button.setText('● ' + text)
        button.setToolTip(detail)
        button.setAccessibleName(text + '. ' + detail)
        button.setStyleSheet(TOOL_STYLE + 'QToolButton { color: ' + self._colours()[colour] + '; }')

    def refresh_status(self, *_):
        tab = self.tabs.currentWidget()
        if tab is None:
            return
        state = tab.transport.state
        scope = 'sender' if tab.kind == 'TCP' else 'broker'
        label = 'Sender' if tab.kind == 'TCP' else 'Broker'
        address = f'{tab.host.text()}:{tab.port.value()}'
        colour = 'green' if state == 'connected' else ('amber' if state in ('connecting', 'disconnecting') else 'gray')
        text = f'{label} :{tab.port.value()}' if state == 'connected' else f'{label} · {state.capitalize()}'
        detail = f'{label}: {address}\nState: {state}'
        if scope in tab.errors:
            code, message = tab.errors[scope]
            text, colour = f'{label} · {code}', 'red'
            detail += f'\n{code}: {message}'
        self._indicator(self.primary_status, text, colour, detail)
        if tab.kind == 'TCP':
            state = tab.listener.state
            text = ('Listener · Stopped' if state == 'stopped' else
                    f'Listener :{tab.listen_port.value()} · ' + ('1 peer' if state == 'connected' else 'Waiting'))
            colour = 'gray' if state == 'stopped' else 'green'
            detail = f'Bind: {tab.listen_host.text()}:{tab.listen_port.value()}\nState: {state}'
            if state == 'connected':
                detail += '\nProducer: ' + tab.listener.origin
            if 'listener' in tab.errors:
                code, message = tab.errors['listener']
                text, colour = 'Listener · ' + code, 'red'
                detail += f'\n{code}: {message}'
        else:
            state = tab.transport.subscription_state
            colour = {'idle':'gray', 'subscribing':'amber', 'unsubscribing':'amber', 'active':'green', 'rejected':'red'}[state]
            text = {'idle':'Subscription · Inactive', 'subscribing':'Subscription · Subscribing…',
                    'unsubscribing':'Subscription · Unsubscribing…', 'active':'Subscribed', 'rejected':'Subscription · Rejected'}[state]
            value = tab.transport.active_filter or tab.subscribe_filter.text()
            if state == 'active':
                short = value if len(value) <= 25 else value[:22] + '…'
                text += ' · ' + short
            detail = f'State: {state}\nFilter: {value}\nUses the same broker connection as publishing.'
            if 'subscription' in tab.errors:
                code, message = tab.errors['subscription']
                text, colour = 'Subscription · ' + code, 'red'
                detail += f'\n{code}: {message}'
        self._indicator(self.secondary_status, text, colour, detail)
        progress = tab.replay.progress
        if tab.replay.active:
            short = f'{min(tab.replay.index + 1, len(tab.replay.steps))}/{len(tab.replay.steps)} · {tab.replay.state.capitalize()}'
        else:
            short = tab.replay.state.capitalize() if tab.replay.state != 'idle' else 'Ready'
        self.trace_status.setText('Trace · ' + short)
        self.trace_status.setToolTip(progress)
        self.trace_status.setStyleSheet(TOOL_STYLE + ('QToolButton { color:' + self._colours()['red'] + '; }' if tab.replay.state == 'error' else ''))
        self.problems_button.setText(f'⨯ {tab.problems.count()}')
        self.problems_button.setStyleSheet(TOOL_STYLE + ('QToolButton { color:' + self._colours()['red'] + '; }' if tab.problems.count() else ''))
        self.output_button.setChecked(not tab.panel.isHidden() and tab.panel_tabs.currentIndex() == 1)
        for index, item in enumerate((self.tcp, self.mqtt)):
            if item.problems.count():
                pixmap = QPixmap(12, 12)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(self._colours()['red']))
                painter.drawEllipse(3, 3, 6, 6)
                painter.end()
                self.tabs.setTabIcon(index, QIcon(pixmap))
            else:
                self.tabs.setTabIcon(index, QIcon())

    def _trace_focus(self):
        tab = self.tabs.currentWidget()
        tab.mode.setCurrentIndex(1)
        if 'trace' in tab.errors:
            tab.reveal_panel(0)
        else:
            tab.trace_editor.setFocus()

    def _toggle_output(self):
        tab = self.tabs.currentWidget()
        if not tab.panel.isHidden() and tab.panel_tabs.currentIndex() == 1:
            tab.toggle_panel()
        else:
            tab.reveal_panel(1)

    def _menus(self):
        file_menu = self.menuBar().addMenu('File')
        for label, shortcut, method in [('Open Trace…', 'Ctrl+O', 'open_trace'), ('Save Trace…', 'Ctrl+S', 'save_trace')]:
            action = QAction(label, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, name=method: getattr(self.tabs.currentWidget(), name)())
            file_menu.addAction(action)
        file_menu.addSeparator()
        quit_action = QAction('Quit Liminal Desk', self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        edit_menu = self.menuBar().addMenu('Edit')
        for title, shortcut, method in [('Undo', QKeySequence.StandardKey.Undo, 'undo'),
                ('Redo', QKeySequence.StandardKey.Redo, 'redo'), ('Cut', QKeySequence.StandardKey.Cut, 'cut'),
                ('Copy', QKeySequence.StandardKey.Copy, 'copy'), ('Paste', QKeySequence.StandardKey.Paste, 'paste'),
                ('Select All', QKeySequence.StandardKey.SelectAll, 'selectAll')]:
            action = QAction(title, self)
            action.setShortcut(shortcut)
            action.triggered.connect(lambda checked=False, name=method: self._edit(name))
            edit_menu.addAction(action)
        payload = self.menuBar().addMenu('Payload')
        for title, shortcut, method in [('Format JSON', 'Ctrl+Shift+F', 'format_current'), ('Send / Run', 'Ctrl+Return', 'send')]:
            action = QAction(title, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, name=method: getattr(self.tabs.currentWidget(), name)())
            payload.addAction(action)
        view = self.menuBar().addMenu('View')
        toggle = QAction('Toggle Output', self)
        toggle.setShortcut(QKeySequence('Ctrl+J'))
        toggle.triggered.connect(self._toggle_output)
        view.addAction(toggle)

    def _edit(self, method):
        focus = QApplication.focusWidget()
        if focus is not None and hasattr(focus, method):
            getattr(focus, method)()

    def restore_preferences(self):
        if self.preferences_path is None:
            return
        try:
            data = json.loads(self.preferences_path.read_text())
            geometry = data.get('geometry')
            if isinstance(geometry, list) and len(geometry) == 4 and all(type(value) is int for value in geometry):
                screen = QApplication.primaryScreen().availableGeometry()
                rectangle = QRect(*geometry)
                for candidate in QApplication.screens():
                    if candidate.availableGeometry().intersects(rectangle):
                        screen = candidate.availableGeometry()
                        break
                width = max(self.minimumWidth(), min(rectangle.width(), screen.width()))
                height = max(self.minimumHeight(), min(rectangle.height(), screen.height()))
                self.setGeometry(max(screen.left(), min(rectangle.x(), screen.right() - width + 1)),
                                 max(screen.top(), min(rectangle.y(), screen.bottom() - height + 1)), width, height)
            for name, tab in [('tcp', self.tcp), ('mqtt', self.mqtt)]:
                prefs = data.get(name, {})
                for key, widget in [('workspace', tab.workspace), ('panel_sizes', tab.splitter), ('messages', tab.viewer.splitter)]:
                    sizes = prefs.get(key)
                    if isinstance(sizes, list) and len(sizes) == 2 and all(type(v) is int and 0 <= v <= 100000 for v in sizes):
                        widget.setSizes(sizes)
                        if key == 'panel_sizes':
                            tab._panel_sizes = sizes
                tab.panel.setVisible(prefs.get('panel_visible') is True)
                tab.panel_tabs.setCurrentIndex(1 if prefs.get('panel_tab') == 1 else 0)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save_preferences(self):
        if self.preferences_path is None:
            return
        rect = self.geometry()
        data = {'geometry': [rect.x(), rect.y(), rect.width(), rect.height()]}
        for name, tab in [('tcp', self.tcp), ('mqtt', self.mqtt)]:
            data[name] = {'workspace': tab.workspace.sizes(), 'panel_sizes': tab.splitter.sizes() if not tab.panel.isHidden() else tab._panel_sizes,
                          'messages': tab.viewer.splitter.sizes(), 'panel_visible': not tab.panel.isHidden(),
                          'panel_tab': tab.panel_tabs.currentIndex()}
        try:
            path = self.preferences_path
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            pass

    def closeEvent(self, event):
        if not self.closing:
            if not self.tcp.confirm_trace_replace() or not self.mqtt.confirm_trace_replace():
                event.ignore()
                return
            self.save_preferences()
            self.closing = True
            self.tcp.shutdown()
            self.mqtt.shutdown()
            self.close_deadline = time.monotonic() + 6
        if self.mqtt.transport.is_running() and time.monotonic() < self.close_deadline:
            event.ignore()
            self.centralWidget().setEnabled(False)
            self.statusBar().showMessage('Closing connections…')
            self.close_timer.start()
        else:
            self.close_timer.stop()
            event.accept()

    def _finish_close(self):
        if not self.mqtt.transport.is_running() or time.monotonic() >= self.close_deadline:
            self.close()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'primary_status'):
            self.refresh_status()
