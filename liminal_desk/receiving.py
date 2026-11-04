"""Bounded received-message storage and a framed TCP listener."""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import struct
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer


@dataclass
class ReceivedMessage:
    raw: bytes
    origin: str
    arrival: datetime = field(default_factory=lambda: datetime.now().astimezone())
    qos: object = None
    retained: object = None


class MessageHistory:
    def __init__(self, maximum_count=500, maximum_bytes=10 * 1024 * 1024):
        self.messages = deque()
        self.maximum_count = maximum_count
        self.maximum_bytes = maximum_bytes
        self.total_bytes = 0

    def append(self, message):
        self.messages.append(message)
        self.total_bytes += len(message.raw)
        removed = 0
        while (len(self.messages) > self.maximum_count or self.total_bytes > self.maximum_bytes):
            self.total_bytes -= len(self.messages.popleft().raw)
            removed += 1
        return removed

    def clear(self):
        self.messages.clear()
        self.total_bytes = 0


class ReceiveInbox:
    """Bound the cross-thread backlog as well as the visible history."""
    def __init__(self):
        self.history = MessageHistory(128, 10 * 1024 * 1024)
        self.lock = threading.Lock()
        self.dropped = 0

    def put(self, message):
        with self.lock:
            self.dropped += self.history.append(message)

    def take(self, count=16):
        with self.lock:
            result = []
            for _ in range(min(count, len(self.history.messages))):
                message = self.history.messages.popleft()
                self.history.total_bytes -= len(message.raw)
                result.append(message)
            dropped, self.dropped = self.dropped, 0
            return result, dropped


class TcpListener(QObject):
    state_changed = Signal(str)
    activity = Signal(str)
    message_received = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.peer = None
        self.state = 'stopped'
        self.maximum = 1048576
        self.buffer = bytearray()
        self.expected = None
        self.origin = ''

    def _state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def start(self, host, port, maximum=1048576):
        if self.server.isListening():
            raise ValueError('Stop the listener before changing its settings.')
        address = QHostAddress(host.strip())
        if address.isNull():
            raise ValueError('Enter a local bind IP address, such as 127.0.0.1 or 0.0.0.0.')
        if not 1 <= int(port) <= 65535:
            raise ValueError('Listener port must be between 1 and 65535.')
        if not 1 <= maximum <= 0xFFFFFFFF:
            raise ValueError('Receive limit must be between 1 and 4,294,967,295 bytes.')
        self.maximum = maximum
        if not self.server.listen(address, int(port)):
            raise ValueError('Cannot start TCP listener: ' + self.server.errorString())
        self._state('listening')
        self.activity.emit(f'Listening for Liminal TCP output on {host}:{self.server.serverPort()}.')

    def _accept(self):
        while self.server.hasPendingConnections():
            peer = self.server.nextPendingConnection()
            if self.peer is not None:
                self.activity.emit('Rejected an additional TCP producer; one is already connected.')
                peer.abort()
                peer.deleteLater()
                continue
            self.peer = peer
            self.origin = f'{peer.peerAddress().toString()}:{peer.peerPort()}'
            self.buffer.clear()
            self.expected = None
            peer.setReadBufferSize(min(self.maximum + 4, 262144))
            peer.readyRead.connect(lambda p=peer: self._read(p))
            peer.disconnected.connect(lambda p=peer: self._read(p))
            peer.errorOccurred.connect(lambda error, p=peer: self._peer_error(p, error))
            self._state('connected')
            self.activity.emit(f'TCP producer connected: {self.origin}.')
            self._read(peer)

    def _peer_error(self, peer, error):
        if peer is self.peer and error != QAbstractSocket.SocketError.RemoteHostClosedError:
            self.activity.emit('ERROR: TCP receive connection: ' + peer.errorString())
            self._release(peer)

    def _read(self, peer):
        if peer is not self.peer:
            return
        # Limit work per event so a busy stream cannot monopolise the UI.
        frames, read_bytes = 0, 0
        while frames < 32 and read_bytes < 262144:
            target = 4 if self.expected is None else self.expected
            needed = target - len(self.buffer)
            if needed:
                chunk = bytes(peer.read(min(needed, 65536)))
                if not chunk:
                    break
                self.buffer.extend(chunk)
                read_bytes += len(chunk)
                if len(self.buffer) < target:
                    continue
            if self.expected is None:
                self.expected = struct.unpack('>I', self.buffer)[0]
                self.buffer.clear()
                if self.expected > self.maximum:
                    self.activity.emit(f'ERROR: TCP frame declares {self.expected:,} bytes; receive limit is {self.maximum:,}. Peer closed.')
                    self._release(peer)
                    return
            else:
                self.message_received.emit(ReceivedMessage(bytes(self.buffer), self.origin))
                self.buffer.clear()
                self.expected = None
                frames += 1
        if peer is not self.peer:
            return
        if peer.bytesAvailable() or (self.expected == 0):
            QTimer.singleShot(0, lambda p=peer: self._read(p))
        elif peer.state() == QAbstractSocket.SocketState.UnconnectedState:
            if self.buffer or self.expected is not None:
                self.activity.emit('ERROR: TCP producer disconnected with an incomplete frame; partial data discarded.')
            self._release(peer)

    def _release(self, peer):
        if peer is not self.peer:
            return
        self.peer = None
        self.buffer.clear()
        self.expected = None
        peer.abort()
        peer.deleteLater()
        self._state('listening' if self.server.isListening() else 'stopped')
        self.activity.emit('TCP producer disconnected.')

    def stop(self):
        self.server.close()
        if self.peer is not None:
            if self.buffer or self.expected is not None:
                self.activity.emit('Incomplete TCP frame discarded when listener stopped.')
            self._release(self.peer)
        self._state('stopped')

    def shutdown(self):
        self.stop()
