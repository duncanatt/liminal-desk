"""Send through both GUI tabs into an actual Liminal process.

Usage: QT_QPA_PLATFORM=offscreen venv/bin/python tests/integration_liminal.py /path/to/liminal
Uses temporary configuration and the local MQTT wire fixture; no user config changes.
"""
from pathlib import Path
import os
import json
import socket
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from liminal_desk.window import MainWindow
from smoke_qt import wait_for, available_port
from wire_broker import WireBroker


def main(binary):
    binary = Path(binary).resolve()
    if not binary.is_file():
        raise SystemExit(f'Liminal executable does not exist: {binary}')
    broker = WireBroker()
    tcp_port = available_port()
    receive_port = available_port()
    logs = []
    process = None
    window = MainWindow(preferences_path=False)
    try:
        window.tcp.listen_port.setValue(receive_port)
        window.tcp.start_listener_button.click()
        assert window.tcp.listener.state == 'listening'
        window.mqtt.port.setValue(broker.port)
        window.mqtt.subscribe_filter.setText('liminal/desk-output')
        window.mqtt.connect_button.click()
        wait_for(lambda: window.mqtt.transport.state == 'connected')
        window.mqtt.subscribe_button.click()
        wait_for(lambda: window.mqtt.transport.subscription_state == 'active')
        with tempfile.TemporaryDirectory(prefix='liminal-desk-check-') as directory:
            config = Path(directory) / 'config.toml'
            config.write_text(f'''
[inputs.tcp_test]
type = "tcp_input"
output = "tcp_data"
channel = {{ type = "broadcast", capacity = 100 }}
parameters = {{ mode = "server", host = "127.0.0.1", port = {tcp_port}, reconnect = true }}

[inputs.mqtt_test]
type = "mqtt_sub"
output = "mqtt_data"
channel = {{ type = "broadcast", capacity = 100 }}
parameters = {{ broker_url = "mqtt://127.0.0.1:{broker.port}", client_id = "liminal_desk_integration", topics = ["liminal/desk-test"], qos = 0 }}

[pipelines.default_pipeline]
description = "Liminal Desk integration check"
[pipelines.default_pipeline.stages]

[outputs.received]
type = "console"
inputs = ["tcp_data", "mqtt_data"]

[outputs.tcp_to_desk]
type = "tcp_output"
inputs = ["tcp_data", "mqtt_data"]
parameters = {{ mode = "client", host = "127.0.0.1", port = {receive_port}, reconnect = true, reconnect_interval_ms = 100 }}

[outputs.mqtt_to_desk]
type = "mqtt_pub"
inputs = ["tcp_data", "mqtt_data"]
parameters = {{ broker_url = "mqtt://127.0.0.1:{broker.port}", client_id = "liminal_desk_output", default_topic = "liminal/desk-output", qos = 0, clean_session = true }}
''')
            process = subprocess.Popen([str(binary), '--config', str(config), '--log-level', 'debug'],
                                       cwd=directory, env={**os.environ, 'RUST_LOG': 'debug'},
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True)
            def read_logs():
                for line in process.stdout:
                    logs.append(line)
            reader = threading.Thread(target=read_logs, daemon=True)
            reader.start()
            wait_for(lambda: any('TCP server listening' in line for line in logs) and
                     any('Subscribed to MQTT topic' in line for line in logs), 12)
            tcp = window.tcp
            tcp.host.setText('127.0.0.1')
            tcp.port.setValue(tcp_port)
            tcp.connect_button.click()
            wait_for(lambda: tcp.transport.state == 'connected')
            tcp.editor.setPlainText('{"desk_check":"TCP_FIRST","message":"Ħello 🌍"}')
            tcp.send_button.click()
            wait_for(lambda: any('=> Message(' in line and 'TCP_FIRST' in line for line in logs))
            wait_for(lambda: not tcp.transport.busy)
            tcp.editor.setPlainText('{"desk_check":"TCP_SECOND"}')
            tcp.send_button.click()
            wait_for(lambda: any('=> Message(' in line and 'TCP_SECOND' in line for line in logs))
            mqtt = window.mqtt
            mqtt.host.setText('127.0.0.1')
            mqtt.port.setValue(broker.port)
            mqtt.topic.setText('liminal/desk-test')
            mqtt.editor.setPlainText('{"desk_check":"MQTT_MESSAGE","message":"Ħello 🌍"}')
            mqtt.send_button.click()
            wait_for(lambda: any('=> Message(' in line and 'MQTT_MESSAGE' in line for line in logs))
            markers = {'TCP_FIRST', 'TCP_SECOND', 'MQTT_MESSAGE'}
            def received_markers(viewer, envelope=False):
                values = [json.loads(message.raw) for message in viewer.history.messages]
                return {value['payload']['desk_check'] if envelope else value['desk_check'] for value in values}
            wait_for(lambda: received_markers(tcp.viewer, True) == markers and
                     received_markers(mqtt.viewer) == markers, 8)
            for message in tcp.viewer.history.messages:
                value = json.loads(message.raw)
                assert {'source', 'topic', 'payload', 'timestamp'} <= value.keys()
            for message in mqtt.viewer.history.messages:
                assert message.origin == 'liminal/desk-output'
                assert 'payload' not in json.loads(message.raw)
            assert tcp.transport.state == 'connected'
            assert tcp.listener.state == 'connected'
            assert mqtt.transport.subscription_state == 'active'
            for tab, prefix in [(tcp, 'TCP_TRACE'), (mqtt, 'MQTT_TRACE')]:
                tab.mode.setCurrentIndex(1)
                tab.trace_editor.setPlainText(json.dumps({'version':1, 'steps':[
                    {'type':'send', 'payload':{'desk_check':prefix + '_FIRST'}},
                    {'type':'delay', 'duration_ms':100},
                    {'type':'send', 'payload':{'desk_check':prefix + '_SECOND'}}]}))
                tab.trace_editor.validate()
                tab.run_button.click()
            markers |= {'TCP_TRACE_FIRST', 'TCP_TRACE_SECOND', 'MQTT_TRACE_FIRST', 'MQTT_TRACE_SECOND'}
            wait_for(lambda: tcp.replay.state == mqtt.replay.state == 'completed' and
                     received_markers(tcp.viewer, True) == markers and received_markers(mqtt.viewer) == markers, 8)
            print('PASS: actual Liminal received single messages and concurrent TCP/MQTT traces; Desk received all seven messages on both output paths.')
            print('Verified persistent TCP envelope framing and MQTT payload-only output on a separate subscription topic.')
            print('MQTT transport used the local wire-test broker; no external broker or configuration was changed.')
            for line in logs:
                if '=> Message(' in line and 'desk_check' in line:
                    print(line.strip())
    except Exception:
        print(''.join(logs[-60:]))
        raise
    finally:
        for tab in (window.tcp, window.mqtt):
            tab.trace_saved_text = tab.trace_editor.toPlainText()
            tab.shutdown()
        wait_for(lambda: not window.mqtt.transport.is_running(), 7)
        window.close()
        broker.close()
        if process:
            process.terminate()
            try:
                process.wait(3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(2)
            process.stdout.close()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
