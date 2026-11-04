# Liminal Desk

Liminal Desk is a small native desktop application for sending JSON to Liminal, inspecting its output, and replaying sequences of events. It brings TCP and MQTT testing into one Python/PySide6 interface.

**Version 1.2** · macOS interface · TCP and MQTT · JSON trace replay

## Contents

- [Requirements](#requirements)
- [Installation and launch](#installation-and-launch)
- [Quick start: a complete round trip](#quick-start-a-complete-round-trip)
- [TCP](#tcp)
- [MQTT](#mqtt)
- [JSON messages and received data](#json-messages-and-received-data)
- [Trace replay](#trace-replay)
- [Status bar and diagnostics](#status-bar-and-diagnostics)
- [Preferences and saved data](#preferences-and-saved-data)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)

## Requirements

- **macOS 13 or later** for the pinned macOS PySide6 packages.
- **Python 3.9–3.14**, as supported by the pinned PySide6 dependency.
- A separately installed/running **Liminal** instance for testing Liminal pipelines.
- An **MQTT broker** for MQTT connections. The included round-trip configuration also requires a broker because it enables both protocols.

Python dependencies are pinned in [requirements.txt](requirements.txt):

```text
PySide6==6.10.3
paho-mqtt==2.1.0
```

The interface uses native Qt widgets. It does not use Electron or an embedded browser. Liminal Desk does not start Liminal or an MQTT broker automatically.

## Installation and launch

Clone or download this repository, open Terminal, and enter the project directory. For a checkout named `liminal-desk`:

```sh
cd liminal-desk
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m liminal_desk
```

Virtual-environment activation is optional because these commands invoke its Python executable directly. Create your own `venv` after downloading the project; virtual environments are excluded from Git.

For subsequent launches, run this from the project directory:

```sh
venv/bin/python -m liminal_desk
```

On macOS, you can also double-click **Liminal Desk.command** in Finder after completing installation. If its executable permission was lost during download, restore it with:

```sh
chmod +x "Liminal Desk.command"
```

The Finder launcher opens Terminal while the application runs. Close the application window or choose Quit to exit. Unsaved trace edits prompt Save / Discard / Cancel.

## Quick start: a complete round trip

Liminal Desk includes an optional configuration example at [`examples/liminal-roundtrip.toml`](examples/liminal-roundtrip.toml), inside this repository. This example was added for Desk testing; it is not a file supplied by the Liminal repository. It forwards incoming messages unchanged to both a TCP output and an MQTT output. It does not calculate IMU properties or modify your existing Liminal configuration. You can instead use your own configuration, adjusting the addresses, channels, and topics accordingly.

Its connections are:

| Operation | Endpoint or topic |
| --- | --- |
| Desk sends TCP to Liminal | `127.0.0.1:9999` |
| Liminal sends TCP to Desk | `127.0.0.1:10000` |
| MQTT broker | `127.0.0.1:1883` |
| Liminal subscribes to input | `liminal/sensors/esp32-001/imu` |
| Liminal publishes output | `liminal/desk/output` |

1. Start an MQTT broker listening on `127.0.0.1:1883`.
2. Launch Liminal Desk. In **TCP → Receive**, click **Start Listening** using `127.0.0.1:10000`.
3. In the **MQTT** tab, connect to `127.0.0.1:1883`, enter `liminal/desk/output` in the receive filter, and click **Subscribe**.
4. From the Liminal Desk project directory, start Liminal with the example:

   ```sh
   liminal --config examples/liminal-roundtrip.toml
   ```

   If `liminal` is not on your PATH, replace it with the path to your Liminal executable. Run this command from the **Liminal Desk** directory, where the example resides. If running from the Liminal directory or elsewhere, pass the full path to `liminal-desk/examples/liminal-roundtrip.toml` instead. There is no requirement to copy this file into Liminal.

5. In Desk's **TCP** tab, connect the sender to `127.0.0.1:9999`. In **Message** mode, click **Send** with the default payload:

   ```json
   {"test": "data", "value": 42}
   ```

6. Inspect the result in both receive views. TCP displays Liminal's JSON envelope; MQTT displays the payload itself.
7. To test MQTT input too, publish the default IMU message to `liminal/sensors/esp32-001/imu`. The example forwards it to the same two outputs.

Use a separate input and output topic when adapting this example. Subscribing to `liminal/#` also shows matching input traffic, including your own publishes.

## TCP

The TCP tab has independent sending and receiving operations. They can run simultaneously.

### Send messages to Liminal

1. Configure Liminal's `tcp_input` in **server** mode.
2. Enter its hostname/IP and port in Desk. The defaults are `127.0.0.1:9999`.
3. Leave **Connection settings → Keep connection open** enabled and click **Connect**.
4. Choose **Message**, edit the JSON, and click **Send**. Successive messages reuse the connection.

For one message per connection, disconnect and turn off **Keep connection open**. **Send** then connects, transmits one frame, and closes. Trace replay requires a persistent connection.

### Receive output from Liminal

1. Enter a local bind IP and port in **Receive**. The defaults are `127.0.0.1:10000`.
2. Click **Start Listening**.
3. Configure Liminal's `tcp_output` in **client** mode to connect to this endpoint. Its `inputs` must name the channels whose messages you want to receive.

The listener accepts one producer at a time, remains listening after that producer disconnects, and rejects additional simultaneous producers. **Stop Listening** closes the listener and its accepted connection while leaving the sender independent.

Liminal's output is a separate stream from the input connection. It contains an envelope with `source`, `topic`, `payload`, and `timestamp`; Desk displays the whole envelope. It is not an acknowledgement of a particular send.

### TCP framing and limits

Each message is encoded as:

```text
[4-byte unsigned big-endian payload length][UTF-8 JSON payload]
```

The length counts payload **bytes**, excluding the prefix. Message mode preserves the editor's text and adds no terminator or extra newline. The receiver handles split headers/bodies and multiple frames arriving together.

Send and receive limits default to **1,048,576 bytes (1 MiB)**. Change them in **Connection settings** or **Receive settings** while the corresponding operation is stopped. Oversized received frames close the peer connection before their declared body is allocated. Truncated frames produce a diagnostic.

A successful send confirms local transmission, not Liminal's processing. Failed operations are not automatically replayed.

## MQTT

The MQTT tab uses **one broker connection** for both publishing and subscribing.

### Publish messages

1. Enter the broker hostname/IP and port. Defaults: `127.0.0.1:1883`. Use a host without an `mqtt://` prefix.
2. If needed, enter credentials under **Connection settings**. Passwords are masked and not saved automatically.
3. Click **Connect** and wait for broker acceptance.
4. Select or type a **Publish topic**. The default is `liminal/sensors/esp32-001/imu`; it must match a subscription configured in Liminal.
5. Choose **Message**, edit the JSON, and click **Publish**.

The initial payload is a full MPU6500-style IMU example containing device identifiers, acceleration, gyroscope, temperature, and a timestamp. Its values and timestamp are fixed and editable; they are not generated from hardware or randomised. See [requirements.md](requirements.md) for the full default payload.

Desk uses **MQTT 3.1.1**, publish **QoS 0**, **Retain off**, and a unique client ID for each connection. MQTT payloads have no Liminal TCP prefix. QoS 0 supplies no per-publish broker acknowledgement or confirmation that Liminal received the message.

### Subscribe to output

1. Choose or enter a receive filter matching Liminal's output topic.
2. Click **Subscribe** and wait for the broker's acknowledgement.
3. Inspect matching messages in the received-message list.

The default filter is `liminal/#`. For outputs such as `factory/accelerator_data` and `factory/tcp_data`, use `factory/#` instead. Suggestions are editable conveniences; output topics depend on Liminal's `default_topic` or `topic_map` configuration.

| Field | Wildcards |
| --- | --- |
| Publish topic | `+` and `#` are rejected |
| Subscribe filter | `+` matches one complete level; `#` matches remaining levels and must be last |

Unsubscribe before changing an active filter. Publishing remains available while subscribed. Reconnecting requires an explicit new Subscribe action; messages and subscriptions are not automatically replayed/restored.

Messages show the actual topic, local arrival time, size, delivered QoS, and retained flag. A broad filter can receive other publishers' messages and Desk's own publishes; Desk does not infer their source or correlate them with earlier sends.

The MQTT receive limit defaults to **1 MiB**, editable while disconnected under Connection settings. Larger payloads are skipped with diagnostics. Incoming history and the cross-thread receive backlog are bounded.

## JSON messages and received data

Outgoing editors provide syntax highlighting, indentation, formatting, and validation while editing and before transmission. Invalid JSON is never sent. Errors have red squiggles, gutter markers, hover explanations, and clickable Problems entries. **Format JSON** preserves numeric literals, escapes, key order, and duplicate payload keys.

Each protocol has its own received-message history and read-only detail viewer:

- Select a row to inspect its contents and metadata.
- Toggle **Formatted JSON** to change the displayed representation.
- **Copy Raw** preserves the original UTF-8 text/bytes. For invalid UTF-8, it supplies exact bytes as `application/octet-stream` clipboard data and hexadecimal text for ordinary text pasting.
- **Copy JSON** copies formatted, valid JSON. Malformed JSON remains visible with a diagnostic; invalid UTF-8 appears as hexadecimal.
- **Follow latest** selects and reveals new arrivals. It starts off; selecting an older message disables it.
- **Clear** empties that received history without disconnecting or unsubscribing.

Histories retain at most **500 messages and 10 MiB of raw payload per tab**, removing the oldest first. Incoming messages never replace outgoing editor contents. Drag the workspace and message-view dividers to adjust space; shorter windows allow the receive section to scroll.

## Trace replay

Choose **Trace** in either tab's sending-mode selector. Message and Trace retain separate editor contents in memory. A JSON array in Message mode is still an ordinary payload.

A trace contains a format version and ordered send/delay steps:

```json
{
  "version": 1,
  "steps": [
    {"type": "send", "payload": {"test": "first", "value": 42}},
    {"type": "delay", "duration_ms": 1000},
    {"type": "send", "payload": {"test": "second", "value": 43}}
  ]
}
```

Only each send step's `payload` is transmitted. Delay steps run inside Desk; the trace wrapper and delay instructions are never sent to Liminal. TCP framing and MQTT publishing settings remain unchanged.

### Run a trace

1. Connect the sender. TCP requires **Keep connection open**; MQTT requires an accepted broker connection and a valid selected publish topic.
2. Edit a trace or click **Open**. Included examples:
   - [TCP trace](examples/tcp-trace.json)
   - [IMU trace](examples/imu-trace.json)
3. Click **Run**. Progress appears in the sending area and status bar.
4. Use **Pause / Resume** or **Stop** as needed. Receivers remain active, and TCP/MQTT traces can run independently.

A delay starts after the preceding step's local completion. Pausing a delay preserves its remaining time. Pausing during a send lets that send complete before pausing. Stop cancels future steps and waits; it cannot retract a send already in flight.

Trace contents, sending mode, endpoint settings, and the MQTT publish topic are locked during replay. Manual sends cannot interleave. Connection loss or a send failure stops the affected trace, including while paused/waiting. A later explicit Run starts from the first step, with no automatic retry or continuation.

Delays use monotonic, non-blocking timers. They are minimum local waits subject to scheduling latency, not a real-time guarantee or a signal that Liminal finished processing. The example IMU trace contains fixed timestamps and readings.

### Trace validation and files

The format requires exactly `version: 1` and a non-empty `steps` array. Each step contains exactly:

| Step | Required fields |
| --- | --- |
| Send | `"type": "send"`, plus a `payload` of any valid JSON type, including `null` |
| Delay | `"type": "delay"`, plus a non-negative integer `duration_ms` |

Unknown fields/types/versions, duplicate control fields, missing payloads, and negative/fractional/boolean delays are rejected. Desk validates the entire trace and payload sizes before sending any trace event. Extracted payloads preserve numeric literals, escapes, ordering, and duplicate payload keys.

**Open / Save** and the File menu use explicit UTF-8 JSON files. Choose a `.json` filename. Readable invalid drafts can be loaded or saved for correction, with diagnostics. Failed file operations preserve edits, and unsaved replacement/close prompts Save / Discard / Cancel. File actions never start playback automatically.

Looping, speed adjustment, per-step topics/endpoints, and expected-output assertions are not supported in version 1.2.

## Status bar and diagnostics

The bottom status bar follows the selected protocol tab:

| Tab | Independent indicators |
| --- | --- |
| TCP | Sender; Listener waiting or connected to one peer |
| MQTT | Broker connection; subscription state on that shared connection |

Gray means inactive/disconnected, amber means pending, green means healthy, and red means an operation error. Text and tooltips provide details and error codes. A rejected subscription does not turn a healthy broker connection red; a failed TCP sender does not change a healthy listener's state.

Click an indicator to reveal settings or relevant diagnostics. Protocol-tab badges expose unresolved problems even when that tab is inactive.

- **Output** opens/toggles the console. The Problems counter opens **Problems**.
- Drag the panel's top divider to resize it; **×** closes it while the status bar stays visible.
- New errors update indicators without forcing a dismissed panel open.
- Copy/Clear Output and Dismiss for a selected operation error are inside the panel. Clearing Output does not clear received messages or resolve errors.
- Problems entries navigate to editor errors or connection details. Correcting JSON does not erase unrelated network errors.

Output retains up to **500 lines per protocol tab**. Application identifiers include `TCP_CONNECT_REFUSED`, `TCP_LISTEN_FAILED`, `TCP_FRAME_TOO_LARGE`, `MQTT_SUB_REJECTED`, `JSON_INVALID`, `TRACE_INVALID`, and `TRACE_SEND_FAILED`. Actual backend codes are included where available.

### macOS shortcuts

| Shortcut | Action |
| --- | --- |
| `⌘Return` | Send/publish in Message mode; Run in Trace mode |
| `⇧⌘F` | Format the current outgoing editor |
| `⌘O` | Open Trace |
| `⌘S` | Save Trace |
| `⌘J` | Toggle Output |
| `⌘Q` | Quit |
| `Tab` / `Shift-Tab` | Indent / unindent in an editable JSON editor |

## Preferences and saved data

Window geometry, divider sizes, and per-tab panel visibility are stored in the project-local `.ui-preferences.json`, excluded from Git. Remove it while the app is closed to reset the layout.

Connection settings, credentials, topics, message contents, and received history are held in memory only. Traces are written to disk only through an explicit Save action. Closing the app releases network connections and replay work.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Application cannot import PySide6 or paho | Install `requirements.txt` using `venv/bin/python`, then launch with that same interpreter. |
| TCP connection refused | Liminal must be listening at the sender's selected address/port. Check its `tcp_input` uses server mode. |
| Listener cannot start | Use a local bind IP and a free port; another app or Desk instance may already own it. |
| Liminal connects to Desk but nothing arrives | An established TCP output connection alone does not imply available messages. Its `inputs` must include the actual channel, such as `raw_tcp_data` or `accelerator_magnitude_data`. |
| MQTT is connected but no messages appear | Subscribe explicitly and wait for acceptance. Match the output topic: `liminal/#` does not match `factory/...`. |
| MQTT displays your own input messages | Narrow the filter to the configured output topic instead of using a broad filter. |
| Liminal reports a missing output `type` | Use `[outputs.name]` (plural), with `type` and `inputs`, followed by `[outputs.name.parameters]`. |
| An MQTT topic mapping falls back to `default_topic` | `topic_map` keys must match input **channel names**, not arbitrary labels. |
| Run is unavailable | Select Trace mode, correct validation errors, connect an idle sender, and enable persistent TCP if applicable. |
| Incoming TCP text does not appear | Send the binary four-byte length prefix as well as JSON. Typing JSON or hexadecimal text into telnet does not supply framing. |

For a quick listener check, start Desk's default TCP listener, then run:

```sh
printf '\000\000\000\035{"test": "data", "value": 42}' | nc -w 1 127.0.0.1 10000
```

This sends a 29-byte JSON payload preceded by its four-byte big-endian length.

## Development and tests

Run these commands from the project directory:

```sh
venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
QT_QPA_PLATFORM=offscreen venv/bin/python -B tests/smoke_qt.py
QT_QPA_PLATFORM=offscreen venv/bin/python -B tests/receive_qt.py
QT_QPA_PLATFORM=offscreen venv/bin/python -B tests/trace_qt.py
```

Tests cover JSON/schema validation, byte-exact framing, subscriptions, malformed input, history limits, replay timing/pause/stop, concurrent protocols, file failures, status indicators, preferences, and shutdown. Network tests use temporary local sockets and a small independent MQTT wire fixture, not an external broker.

For the end-to-end test, supply the path to a built Liminal executable:

```sh
QT_QPA_PLATFORM=offscreen venv/bin/python -B tests/integration_liminal.py /path/to/liminal
```

Replace `/path/to/liminal` with the executable's actual path. The test starts Liminal with temporary configuration, sends single messages and concurrent traces, verifies all seven messages through both output paths, then cleans up its own process and sockets. It does not edit your Liminal configuration.

Version 1.2 was verified on macOS with Python 3.9.6, PySide6 6.10.3, and paho-mqtt 2.1.0: **32 automated checks plus the Liminal integration test passed**. Native light/dark and compact-window layouts were also inspected.

### Project structure

```text
liminal_desk/
  __main__.py       Application entry point
  window.py         Workspace, status bar, diagnostics, files and preferences
  controls.py       Native disclosure and toolbar controls
  editor.py         JSON editing, highlighting and inline diagnostics
  trace.py          Trace parsing and replay state machine
  transports.py     TCP sender and MQTT network worker
  receiving.py      TCP listener and bounded message storage
  message_view.py   Received-message list and read-only viewer
  validation.py    JSON, endpoint, topic and framing validation
  samples.py        Initial examples
examples/           Liminal configuration and trace files
tests/              Validation, GUI, network and integration checks
```

The detailed scope and acceptance criteria are in [requirements.md](requirements.md).

Current transport scope is plain TCP and MQTT 3.1.1 over TCP; TLS, MQTT over WebSockets, multiple MQTT brokers, and automatic request/reply correlation are outside version 1.2.

Library documentation: [Qt for Python](https://doc.qt.io/qtforpython-6/) and [Eclipse Paho MQTT Python](https://eclipse.dev/paho/clients/python/docs/).
