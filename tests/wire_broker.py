"""Small local MQTT 3.1.1 wire fixture for integration tests, not a production broker.

Uses real sockets and independent packet encoding to inspect CONNECT/PUBLISH,
route publishes to subscribers, and acknowledge subscriptions.
"""
import socket
import struct
import threading


def packet(kind, body):
    length = len(body)
    encoded = bytearray([kind])
    while True:
        digit = length % 128
        length //= 128
        encoded.append(digit | (128 if length else 0))
        if not length:
            break
    return bytes(encoded) + body


def exact(sock, count):
    result = b''
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise EOFError()
        result += chunk
    return result


def receive(sock):
    header = exact(sock, 1)[0]
    length, multiplier = 0, 1
    while True:
        digit = exact(sock, 1)[0]
        length += (digit & 127) * multiplier
        if not digit & 128:
            break
        multiplier *= 128
        if multiplier > 128**3:
            raise ValueError('Malformed MQTT length')
    return header, exact(sock, length)


class WireBroker:
    def __init__(self, reject=0, acknowledge=True, subscription_code=0, subscription_ack=True, unsubscribe_ack=True):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.reject = reject
        self.acknowledge = acknowledge
        self.subscription_code = subscription_code
        self.subscription_ack = subscription_ack
        self.unsubscribe_ack = unsubscribe_ack
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.clients = []
        self.threads = []
        self.messages = []
        self.connects = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.settimeout(0.2)
            record = {'socket': client, 'topics': set(), 'lock': threading.Lock()}
            with self.lock:
                self.clients.append(record)
            thread = threading.Thread(target=self._client, args=(record,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _send(self, record, data):
        with record['lock']:
            record['socket'].sendall(data)

    def _client(self, record):
        sock = record['socket']
        try:
            # Blocking reads are interrupted by shutdown() during cleanup.
            sock.settimeout(None)
            while not self.stop.is_set():
                header, body = receive(sock)
                kind = header >> 4
                if kind == 1:
                    self.connects.append(body)
                    if self.acknowledge:
                        self._send(record, packet(0x20, bytes([0, self.reject])))
                    if self.reject:
                        break
                elif kind == 8:
                    pos, granted = 2, []
                    while pos < len(body):
                        length = struct.unpack('>H', body[pos:pos + 2])[0]
                        pos += 2
                        if self.subscription_code != 128:
                            record['topics'].add(body[pos:pos + length].decode())
                        pos += length
                        granted.append(self.subscription_code)
                        pos += 1
                    if self.subscription_ack:
                        self._send(record, packet(0x90, body[:2] + bytes(granted)))
                elif kind == 3:
                    length = struct.unpack('>H', body[:2])[0]
                    topic = body[2:2 + length].decode()
                    pos = 2 + length
                    qos = (header >> 1) & 3
                    mid = body[pos:pos + 2] if qos else None
                    pos += 2 if qos else 0
                    payload = body[pos:]
                    self.messages.append((topic, payload, qos, bool(header & 1)))
                    self.deliver(topic, payload)
                    if qos == 1:
                        self._send(record, packet(0x40, mid))
                elif kind == 10:
                    pos = 2
                    while pos < len(body):
                        length = struct.unpack('>H', body[pos:pos + 2])[0]
                        pos += 2
                        record['topics'].discard(body[pos:pos + length].decode())
                        pos += length
                    if self.unsubscribe_ack:
                        self._send(record, packet(0xb0, body[:2]))
                elif kind == 12:
                    self._send(record, packet(0xd0, b''))
                elif kind == 14:
                    break
        except (OSError, EOFError, ValueError):
            pass
        finally:
            sock.close()
            with self.lock:
                if record in self.clients:
                    self.clients.remove(record)

    @staticmethod
    def matches(topic_filter, topic):
        if topic.startswith('$') and not topic_filter.startswith('$'):
            return False
        pattern, levels = topic_filter.split('/'), topic.split('/')
        for index, level in enumerate(pattern):
            if level == '#':
                return True
            if index >= len(levels) or (level != '+' and level != levels[index]):
                return False
        return len(pattern) == len(levels)

    def deliver(self, topic, payload, retain=False):
        with self.lock:
            targets = list(self.clients)
        encoded = topic.encode()
        data = packet(0x31 if retain else 0x30, struct.pack('>H', len(encoded)) + encoded + payload)
        for target in targets:
            if any(self.matches(value, topic) for value in tuple(target['topics'])):
                try:
                    self._send(target, data)
                except OSError:
                    pass

    def close_clients(self):
        with self.lock:
            clients = list(self.clients)
        for record in clients:
            try:
                record['socket'].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.close_clients()
        self.thread.join(1)
        for thread in self.threads:
            thread.join(1)
