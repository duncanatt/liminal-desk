"""Bounded received-message list and read-only JSON/raw inspection."""
from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget)
from .editor import JsonEditor, pretty_json
from .receiving import MessageHistory
from .validation import validate_json


class ReceivedModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = MessageHistory()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.history.messages)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ['Received', 'Peer / topic', 'Bytes'][section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= self.rowCount():
            return None
        message = self.history.messages[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return [message.arrival.strftime('%H:%M:%S.%f')[:-3], message.origin, f'{len(message.raw):,}'][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            detail = f'{message.arrival.isoformat(timespec="milliseconds")}\n{message.origin}\n{len(message.raw):,} bytes'
            if message.qos is not None:
                detail += f'\nQoS {message.qos} · Retained: {"yes" if message.retained else "no"}'
            return detail
        return None

    def add(self, message):
        history = self.history
        if len(message.raw) > history.maximum_bytes:
            return False
        removed, total = 0, history.total_bytes
        while (len(history.messages) - removed + 1 > history.maximum_count or
               total + len(message.raw) > history.maximum_bytes):
            total -= len(history.messages[removed].raw)
            removed += 1
        if removed:
            self.beginRemoveRows(QModelIndex(), 0, removed - 1)
            for _ in range(removed):
                history.messages.popleft()
            history.total_bytes = total
            self.endRemoveRows()
        row = self.rowCount()
        self.beginInsertRows(QModelIndex(), row, row)
        history.messages.append(message)
        history.total_bytes += len(message.raw)
        self.endInsertRows()
        return True

    def clear(self):
        self.beginResetModel()
        self.history.clear()
        self.endResetModel()


class MessageViewer(QWidget):
    activity = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected = None
        self._automatic_selection = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        self.count = QLabel('Messages (0)')
        self.count.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.count.setMinimumWidth(110)
        self.clear_button = QPushButton('Clear')
        self.clear_button.setToolTip('Clear Messages')
        self.follow = QCheckBox('Follow latest')
        bar.addWidget(self.count, 1)
        bar.addWidget(self.follow)
        bar.addWidget(self.clear_button)
        layout.addLayout(bar)
        self.model = ReceivedModel(self)
        self.history = self.model.history
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, self.table.fontMetrics().horizontalAdvance('00:00:00.000') + 20)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(85)
        self.table.setAccessibleName('Received messages')
        self.detail = JsonEditor()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(100)
        self.detail.setAccessibleName('Received message contents (read only)')
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter = splitter
        splitter.addWidget(self.table)
        detail_area = QWidget()
        detail_layout = QVBoxLayout(detail_area)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.metadata = QLabel('Select a received message to inspect it.')
        self.metadata.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        detail_layout.addWidget(self.metadata)
        detail_layout.addWidget(self.detail, 1)
        splitter.addWidget(detail_area)
        splitter.setChildrenCollapsible(False)
        splitter.setMinimumHeight(240)
        splitter.setSizes([125, 270])
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.formatted = QCheckBox('Formatted JSON')
        self.formatted.setChecked(True)
        self.copy_raw_button = QPushButton('Copy Raw')
        self.copy_formatted_button = QPushButton('Copy JSON')
        self.copy_formatted_button.setToolTip('Copy Formatted JSON')
        actions.addWidget(self.formatted)
        actions.addStretch()
        actions.addWidget(self.copy_raw_button)
        actions.addWidget(self.copy_formatted_button)
        layout.addLayout(actions)
        self.limits = QLabel('History: 500 messages / 10 MiB · oldest messages removed first')
        self.limits.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.limits.setToolTip(self.limits.text())
        layout.addWidget(self.limits)
        self.table.selectionModel().currentRowChanged.connect(self._select)
        self.formatted.toggled.connect(self._render)
        self.clear_button.clicked.connect(self.clear)
        self.copy_raw_button.clicked.connect(self.copy_raw)
        self.copy_formatted_button.clicked.connect(self.copy_formatted)
        self._render()

    def add_message(self, message):
        was_empty = self.model.rowCount() == 0
        self._automatic_selection = True
        if not self.model.add(message):
            self._automatic_selection = False
            self.activity.emit('ERROR: Message exceeds the 10 MiB history capacity; not stored.')
            return
        if self.selected is not None and not any(item is self.selected for item in self.history.messages):
            self.selected = None
            self._render()
        self.count.setText(f'Messages ({self.model.rowCount()}) · {self.history.total_bytes:,} bytes')
        self.count.setToolTip(self.count.text())
        if self.follow.isChecked() or was_empty:
            self.table.selectRow(self.model.rowCount() - 1)
            self.table.scrollTo(self.model.index(self.model.rowCount() - 1, 0))
        self._automatic_selection = False

    def _select(self, current, previous):
        if not self._automatic_selection and current.isValid() and current.row() < self.model.rowCount() - 1:
            self.follow.setChecked(False)
        self.selected = self.history.messages[current.row()] if current.isValid() and current.row() < self.model.rowCount() else None
        self._render()

    def _raw_text(self):
        if self.selected is None:
            return None
        try:
            return self.selected.raw.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def _render(self, *_):
        text = self._raw_text()
        error = validate_json(text) if text is not None else None
        valid = text is not None and error is None
        self.formatted.setEnabled(valid)
        self.copy_raw_button.setEnabled(self.selected is not None)
        self.copy_formatted_button.setEnabled(valid)
        if self.selected is None:
            self.metadata.setText('Select a received message to inspect it.')
            self.detail.clear()
        else:
            message = self.selected
            info = f'{message.arrival.isoformat(timespec="milliseconds")} · {len(message.raw):,} bytes\n{message.origin}'
            if message.qos is not None:
                info += f'\nQoS {message.qos} · Retained: {"yes" if message.retained else "no"}'
            if text is None:
                info += '\nInvalid UTF-8 — showing hexadecimal bytes.'
                self.detail.setPlainText(message.raw.hex(' '))
            else:
                if error:
                    info += f'\nJSON error: {error.message} (line {error.line}, column {error.column})'
                self.detail.setPlainText(pretty_json(text) if valid and self.formatted.isChecked() else text)
                self.detail.validate()
            self.metadata.setText(info)
            self.metadata.setToolTip(info)
        if self.selected is None or text is None:
            self.detail.timer.stop()
            self.detail.diagnostic = None
            self.detail.setExtraSelections([])
            self.detail.gutter.update()

    def copy_raw(self):
        if self.selected is None:
            return
        mime = QMimeData()
        mime.setData('application/octet-stream', self.selected.raw)
        text = self._raw_text()
        mime.setText(text if text is not None else self.selected.raw.hex(' '))
        QApplication.clipboard().setMimeData(mime)

    def copy_formatted(self):
        text = self._raw_text()
        if text is not None and validate_json(text) is None:
            QApplication.clipboard().setText(pretty_json(text))

    def clear(self):
        self.selected = None
        self.model.clear()
        self.count.setText('Messages (0)')
        self._render()
