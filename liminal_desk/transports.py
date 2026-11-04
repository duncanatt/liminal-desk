"""Asynchronous TCP and a dedicated MQTT network thread; no automatic replay."""
import queue
import threading
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostInfo, QTcpSocket
import paho.mqtt.client as mqtt

from .validation import endpoint, tcp_frame, topic_name, subscription_filter
from .receiving import ReceivedMessage, ReceiveInbox


class TcpTransport(QObject):
    state_changed = Signal(str)
    activity = Signal(str)
    sent = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QTcpSocket(self)
        self.state = 'disconnected'
        self.pending = None
        self.remaining = 0
        self.payload_size = 0
        self.one_shot = False
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.errorOccurred.connect(self._error)
        self.socket.bytesWritten.connect(self._written)
        self.socket.readyRead.connect(self._discard_reply)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.timeout_ms = 5000

    @property
    def busy(self):
        return self.pending is not None or self.remaining > 0

    def _state(self, value):
        self.state = value
        self.state_changed.emit(value)

    def connect_to(self, host, port):
        host, port = endpoint(host, port)
        if self.state != 'disconnected':
            raise ValueError('Disconnect before changing the connection.')
        self._state('connecting')
        self.activity.emit(f'Connecting to {host}:{port}…')
        self.timer.start(self.timeout_ms)
        self.socket.connectToHost(host, port)

    def _connected(self):
        self.timer.stop()
        self._state('connected')
        self.activity.emit('TCP connection established.')
        if self.pending is not None:
            frame = self.pending
            self.pending = None
            self._write(frame)

    def send(self, payload, host, port, persistent=True, maximum=1048576):
        frame = tcp_frame(payload, maximum)
        endpoint(host, port)
        if self.busy or self.state == 'connecting':
            raise ValueError('Wait for the current TCP operation to finish.')
        self.payload_size = len(payload)
        self.one_shot = not persistent
        if self.state == 'connected':
            self._write(frame)
        elif persistent:
            raise ValueError('Connect to the TCP server before sending.')
        else:
            self.pending = frame
            self.connect_to(host, port)

    def _write(self, frame):
        self.remaining = len(frame)
        self.timer.start(self.timeout_ms)
        accepted = self.socket.write(frame)
        # QTcpSocket buffers the frame and handles OS-level partial writes.
        if accepted != len(frame):
            self.activity.emit('ERROR: TCP could not queue the complete frame; delivery is uncertain.')
            self.disconnect()

    def _written(self, count):
        if not self.remaining:
            return
        self.remaining = max(0, self.remaining - count)
        if self.remaining == 0:
            self.timer.stop()
            self.activity.emit(f'Sent {self.payload_size:,} payload bytes + 4 framing bytes; processing unconfirmed.')
            self.sent.emit(self.payload_size)
            if self.one_shot:
                self.socket.disconnectFromHost()

    def _discard_reply(self):
        count = self.socket.bytesAvailable()
        self.socket.readAll()
        if count:
            self.activity.emit(f'Received {count:,} bytes on the sending connection; use the listener to inspect configured Liminal output.')

    def _error(self, _):
        self.activity.emit(f'ERROR: {self.socket.errorString()}. No payload will be replayed.')
        self.disconnect()

    def _timeout(self):
        self.activity.emit('ERROR: TCP operation timed out; delivery may be incomplete. No automatic retry.')
        self.disconnect()

    def _disconnected(self):
        was_active = self.state != 'disconnected'
        interrupted = self.busy
        self.timer.stop()
        self.pending = None
        self.remaining = 0
        self._state('disconnected')
        if was_active:
            self.activity.emit('TCP disconnected.' + (' Pending transmission was interrupted.' if interrupted else ''))

    def disconnect(self):
        self.socket.abort()
        self._disconnected()

    def shutdown(self):
        self.disconnect()


class MqttTransport(QObject):
    message_received = Signal(object)
    subscription_changed = Signal(str)
    state_changed = Signal(str)
    activity = Signal(str)
    sent = Signal(int)
    # Every worker event is marshalled onto the GUI thread. Generation IDs
    # ensure a delayed event from an old connection cannot affect a new one.
    _event = Signal(int, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = 'disconnected'
        self.thread = None
        self.stop = None
        self.commands = None
        self.generation = 0
        self.busy = False
        self.timeout_seconds = 5
        self.subscription_state = 'idle'
        self.active_filter = ''
        self.receive_maximum = 1048576
        self.inbox = ReceiveInbox()
        self.receive_timer = QTimer(self)
        self.receive_timer.setInterval(40)
        self.receive_timer.timeout.connect(self._drain_incoming)
        self.receive_timer.start()
        self._connection_args = None
        self.connection_timer = QTimer(self)
        self.connection_timer.setSingleShot(True)
        self.connection_timer.timeout.connect(self._connect_timeout)
        self._event.connect(self._handle)

    def _state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def connect_to(self, host, port, username='', password=''):
        host, port = endpoint(host, port)
        if self.state != 'disconnected' or self.is_running():
            raise ValueError('Wait for the previous MQTT connection to close.')
        self.generation += 1
        self._subscription('idle')
        self.stop = threading.Event()
        self.commands = queue.Queue()
        self._state('connecting')
        self.activity.emit(f'Connecting to {host}:{port}…')
        self._connection_args = (port, username, password)
        self.connection_timer.start(max(1, int(self.timeout_seconds * 1000)))
        generation = self.generation
        # Qt resolves asynchronously. Passing a numeric address to Paho avoids
        # an unbounded DNS lookup inside the network worker.
        QHostInfo.lookupHost(host, lambda info: self._resolved(generation, info))

    def _resolved(self, generation, info):
        if generation != self.generation or self.state != 'connecting' or self.stop.is_set():
            return
        if info.error() != QHostInfo.HostInfoError.NoError or not info.addresses():
            self.activity.emit('ERROR: MQTT hostname lookup failed: ' + info.errorString())
            self.connection_timer.stop()
            self._connection_args = None
            self.stop.set()
            self._state('disconnected')
            return
        addresses = sorted(info.addresses(), key=lambda a: a.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol)
        port, username, password = self._connection_args
        self._connection_args = None
        self.thread = threading.Thread(
            target=self._run,
            args=(self.generation, self.stop, self.commands, addresses[0].toString(), port, username, password),
            name='LiminalDesk-MQTT', daemon=True)
        self.thread.start()

    def _connect_timeout(self):
        if self.state == 'connecting':
            self.activity.emit('ERROR: MQTT connection timed out. No automatic retry.')
            self.disconnect()

    def _run(self, generation, stop, commands, host, port, username, password):
        emit = lambda kind, data: self._event.emit(generation, kind, data)
        inbox = self.inbox
        maximum = self.receive_maximum
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id='liminal-desk-' + uuid.uuid4().hex[:12],
                             protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        client.connect_timeout = self.timeout_seconds
        if username:
            client.username_pw_set(username, password)
        connected = False
        pending = {}
        control = {}
        deadline = time.monotonic() + self.timeout_seconds
        oversized = 0

        def on_connect(client, userdata, flags, reason, properties):
            nonlocal connected
            if reason.is_failure:
                emit('log', f'ERROR: Broker rejected connection: {reason} (code {reason.value}).')
                stop.set()
            else:
                connected = True
                emit('connected', f'Broker accepted connection: {reason} (code {reason.value}).')

        def on_disconnect(client, userdata, flags, reason, properties):
            if not stop.is_set():
                emit('log', f'ERROR: Broker disconnected: {reason} (code {reason.value}). No automatic retry.')
            stop.set()

        def published(size, topic):
            emit('sent', size)
            emit('log', f'Published {size:,} bytes to {topic!r}; QoS 0, retain off. Delivery unconfirmed.')

        def on_publish(client, userdata, mid, reason, properties):
            item = pending.pop(mid, None)
            if item:
                published(item[0], item[1])

        def on_subscribe(client, userdata, mid, reasons, properties):
            item = control.pop(mid, None)
            if not item:
                return
            if not reasons or any(reason.is_failure for reason in reasons):
                emit('subscription', ('rejected', '', 'Broker rejected subscription: ' + ', '.join(f'{r} (code {r.value})' for r in reasons)))
            else:
                emit('subscription', ('active', item[1], f'Subscribed to {item[1]!r}; broker granted QoS {reasons[0].value}.'))

        def on_unsubscribe(client, userdata, mid, reasons, properties):
            item = control.pop(mid, None)
            if not item:
                return
            if any(reason.is_failure for reason in reasons):
                emit('subscription', ('active', item[1], 'ERROR: Broker rejected unsubscribe.'))
            else:
                emit('subscription', ('idle', '', f'Unsubscribed from {item[1]!r}.'))

        def on_message(client, userdata, message):
            nonlocal oversized
            if len(message.payload) > maximum:
                oversized += 1
                return
            inbox.put(ReceivedMessage(bytes(message.payload), message.topic,
                                      qos=message.qos, retained=bool(message.retain)))

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_publish = on_publish
        client.on_subscribe = on_subscribe
        client.on_unsubscribe = on_unsubscribe
        client.on_message = on_message
        last_report = time.monotonic()
        try:
            client.connect(host, port, keepalive=30)
            while not stop.is_set():
                if not connected and time.monotonic() >= deadline:
                    raise TimeoutError('Broker did not acknowledge the connection in time')
                try:
                    kind, data = commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if kind == 'publish':
                        topic, payload = data
                        info = client.publish(topic, payload, qos=0, retain=False)
                        if info.rc != mqtt.MQTT_ERR_SUCCESS:
                            raise OSError(mqtt.error_string(info.rc))
                        if info.is_published():
                            published(len(payload), topic)
                        else:
                            pending[info.mid] = (len(payload), topic, time.monotonic())
                    else:
                        rc, mid = (client.subscribe(data, qos=0) if kind == 'subscribe' else client.unsubscribe(data))
                        if rc != mqtt.MQTT_ERR_SUCCESS:
                            raise OSError(mqtt.error_string(rc))
                        control[mid] = (kind, data, time.monotonic())
                rc = client.loop(timeout=0.05)
                if rc != mqtt.MQTT_ERR_SUCCESS and not stop.is_set():
                    raise OSError(mqtt.error_string(rc))
                if any(time.monotonic() - item[2] > self.timeout_seconds for item in pending.values()):
                    raise TimeoutError('MQTT publish timed out; delivery is uncertain')
                if any(time.monotonic() - item[2] > self.timeout_seconds for item in control.values()):
                    raise TimeoutError('Broker did not acknowledge subscription operation; connection closed to clear uncertain state')
                if oversized and time.monotonic() - last_report >= 1:
                    emit('log', f'ERROR: Skipped {oversized} MQTT messages exceeding receive limit {maximum:,} bytes.')
                    oversized = 0
                    last_report = time.monotonic()
        except Exception as exc:
            if not stop.is_set():
                emit('log', f'ERROR: MQTT {exc}. No automatic retry or replay.')
        finally:
            if oversized:
                emit('log', f'ERROR: Skipped {oversized} MQTT messages exceeding receive limit {maximum:,} bytes.')
            client.disconnect()
            client.loop(timeout=0.05)
            emit('disconnected', None)

    def _drain_incoming(self):
        messages, dropped = self.inbox.take()
        for message in messages:
            self.message_received.emit(message)
        if dropped:
            self.activity.emit(f'ERROR: Receive backlog full; discarded {dropped} oldest MQTT messages.')

    def _subscription(self, state, topic='', detail=''):
        self.subscription_state = state
        self.active_filter = topic
        self.subscription_changed.emit(state)
        if detail:
            self.activity.emit(('ERROR: ' if state == 'rejected' else '') + detail)

    def _handle(self, generation, kind, data):
        if generation != self.generation:
            return
        if kind == 'connected':
            if self.state == 'connecting':
                self.connection_timer.stop()
                self._state('connected')
                self.activity.emit(data)
        elif kind == 'disconnected':
            self.connection_timer.stop()
            self.busy = False
            self._subscription('idle')
            self._state('disconnected')
            self.activity.emit('MQTT disconnected. Subscribe again after reconnecting.')
        elif kind == 'sent':
            self.busy = False
            self.sent.emit(data)
        elif kind == 'subscription':
            if self.state == 'connected':
                self._subscription(*data)
        elif kind == 'log':
            self.activity.emit(data)

    def _require_connected(self):
        if self.state != 'connected' or self.stop.is_set():
            raise ValueError('Connect to the MQTT broker first.')

    def publish(self, topic, payload):
        topic_name(topic)
        self._require_connected()
        if self.busy:
            raise ValueError('Wait for the current publish to finish.')
        if len(payload) + len(topic.encode('utf-8')) + 2 > 268435455:
            raise ValueError('Payload exceeds the MQTT packet size limit.')
        self.busy = True
        self.commands.put(('publish', (topic, payload)))

    def subscribe(self, topic):
        subscription_filter(topic)
        self._require_connected()
        if self.subscription_state not in ('idle', 'rejected'):
            raise ValueError('Unsubscribe before changing the subscription filter.')
        self._subscription('subscribing', topic)
        self.commands.put(('subscribe', topic))

    def unsubscribe(self):
        self._require_connected()
        if self.subscription_state != 'active':
            raise ValueError('Wait for an active subscription before unsubscribing.')
        topic = self.active_filter
        self._subscription('unsubscribing', topic)
        self.commands.put(('unsubscribe', topic))

    def disconnect(self):
        self.connection_timer.stop()
        self._connection_args = None
        if self.stop:
            self.stop.set()
        self.busy = False
        self._subscription('idle')
        if self.is_running():
            self._state('disconnecting')
        else:
            self._state('disconnected')

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def shutdown(self):
        self.receive_timer.stop()
        self.disconnect()
