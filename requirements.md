# Liminal Desk — requirements contract

Status: Version 1.2 implementation authorised on 2026-11-04 and completed. Native layout/status-bar and trace-replay checks passed alongside existing send/receive regressions; see README.md for verification.

## 1. Purpose

Build a small Python desktop application for manually sending JSON to Liminal and inspecting its output through TCP or an MQTT broker. The application should consolidate the basic sending and receiving workflows currently spread across Postman, Packet Sender, and MQTTX.

Program name: **Liminal Desk**.

This document defines the implemented version 1.2, including the retained version 1/1.1 transport requirements. Version 1.2 explicitly supersedes earlier interface/scope statements only where noted below; existing transport behaviour and defaults are retained. Changes to the agreed scope should be recorded here before implementation.

## 2. Interface

- One window with two tabs: **TCP** and **MQTT**.
- A restrained, macOS-like interface using Python and PySide6: native window controls, system fonts, standard shortcuts, comfortable spacing, and support for the system light/dark appearance.
- Each tab retains its connection settings, explicit Connect/Disconnect controls, connection status, editable JSON payload, and Send or Publish button. Version 1.1 adds sending and receiving sections within these same two tabs; no new top-level protocol tabs are introduced. TCP sending and listening have independent controls. MQTT publishing and subscribing share one broker connection.
- Editors support ordinary editing shortcuts, indentation, and a Format JSON action.
- Both editors provide full JSON syntax highlighting as the user types: object keys, string values (including escape sequences), numbers, booleans, null, and structural punctuation. Colours must remain readable in light and dark appearance.
- Validate JSON as the user edits, after a short pause, and again before transmission or formatting. Present errors in the manner of VS Code: a red squiggly underline at the reported error location, a small red marker in the gutter beside the offending line, and a hover tooltip explaining the error. The Problems tab in the bottom panel beneath each editor shows the diagnostic with an error icon, explanation, and line/column numbers; clicking the entry focuses the editor and moves the cursor to the error. Use inline diagnostics rather than modal error dialogs for malformed JSON. Report the first parser error reliably; listing multiple errors is not required for version 1. For missing text at the end of the payload, mark the insertion point on the final line. Clear stale diagnostics when the payload becomes valid. Invalid JSON remains editable and is never transmitted; a failed Format JSON action must preserve the original text.
- Send the editor contents as UTF-8, preserving whitespace unless Format JSON is explicitly used. Accept any valid JSON value; Liminal-specific fields and schemas are deferred.
- Each connection tab has a resizable, collapsible bottom panel with **Problems** and **Output** tabs, following the VS Code interaction pattern. Output is a bounded log (500 lines) with timestamps, connection events, transmission byte counts, broker acknowledgement/rejection reasons where available, and readable errors. Provide Copy Output and Clear Output actions. This is a read-only diagnostic panel, not a terminal. Version 1.1 displays received messages in a separate message view within the protocol tab, rather than mixing payloads into this diagnostic log.
- Network operations must keep the interface responsive. Disconnecting or closing the window must clean up connections and background work promptly.

## 3. TCP tab

### Send to Liminal — existing

- Enter a server hostname or IP address and port (1–65535). Defaults: `127.0.0.1` and `9999`. Initial JSON payload: `{"test": "data", "value": 42}`.
- The application acts as a TCP client; Liminal must have a TCP input configured in server mode.
- Provide a **Keep connection open** option, enabled by default. Connect once, then send multiple messages over the same connection until Disconnect, remote closure, or an error.
- With this option disabled, Send opens a connection, transmits one complete frame, then closes it. Connection controls must reflect this mode clearly.
- Frame every message as a four-byte unsigned big-endian integer followed immediately by the JSON payload bytes. The integer counts UTF-8 payload bytes only, excluding the prefix. Do not append a terminator or newline beyond text already in the editor.
- Show the payload byte count. Use a configurable maximum payload size, defaulting to 1,048,576 bytes to match Liminal's current default; reject oversized payloads before connecting or sending. The configured limit must also fit an unsigned 32-bit integer.
- Handle partial writes and use finite connection/send timeouts. On failure, show the error and mark the connection disconnected; reconnect manually, without automatically replaying a payload.
- A successful send means the frame was written to the connection. It does not establish that Liminal accepted or processed the JSON. No application-level acknowledgement is assumed.

### Receive from Liminal — version 1.1

- Provide a TCP server/listener with a local bind address and port, and explicit Start Listening / Stop Listening controls. Defaults: `127.0.0.1` and `10000`, separate from the sending endpoint on port `9999`. Lock listener settings while listening.
- Configure Liminal's `tcp_output` separately in client mode to connect to this listener. Liminal output is an independent stream, not a request/reply acknowledgement on Desk's sending connection. Desk does not edit or launch Liminal.
- Display listener state and connected peer address. Support one connected producer at a time; reject additional simultaneous connections with a diagnostic. Keep listening after the producer disconnects so a subsequent connection can be accepted. Stop Listening closes both the listener and any accepted connection.
- Decode each frame as a four-byte unsigned big-endian byte count followed by that many UTF-8 JSON bytes. Buffer incomplete headers/bodies and handle multiple frames arriving in one read. Persistent streams must work without assuming that a socket read equals a message.
- Apply a configurable receive-frame maximum, defaulting to 1,048,576 bytes. Reject an oversized frame before allocating its declared body, log the error, and close that peer connection while leaving the listener running. Discard any incomplete frame on disconnection and report truncation.
- Liminal's TCP output JSON is an envelope containing `source`, `topic`, `payload`, and `timestamp`. Display the entire envelope, including any additional fields, rather than extracting only `payload`.
- Sending and listening must operate simultaneously and independently. Starting a listener must not disconnect the sending client.

Protocol basis: `src/processors/common/tcp.rs`, `src/processors/input/tcp.rs`, and `src/processors/output/tcp.rs`, inspected on 2026-11-04.

## 4. MQTT tab

- Enter broker hostname or IP address and port (1–65535). Defaults: `127.0.0.1` and `1883`. Initial JSON payload: the full IMU example below, with fixed, editable values (including a fixed example timestamp); it is not automatically randomised.
- Enter a publish topic, defaulting to `liminal/sensors/esp32-001/imu`. It must be non-empty and contain no subscription wildcards (`+` or `#`). The topic must match a subscription configured in Liminal. Version 1.1 replaces the text field with an editable dropdown, as specified below.
- Use MQTT 3.1.1 over TCP, with a unique generated client ID and an optional username/password pair. Mask the password and do not save it to disk.
- Connect explicitly and keep the broker connection open for successive publishes. Publish is available only after the broker confirms the connection.
- Publish the editor contents as UTF-8 JSON, with no Liminal TCP length prefix.
- For versions 1 and 1.1, fix publish QoS to 0 and Retain to off, and show these settings in the interface. Report local publish completion without claiming delivery to Liminal.
- On connection loss, show the error and return to a disconnected state. Reconnect manually; do not automatically republish a message.

### Receive from Liminal — version 1.1

- Add a separate Subscribe filter dropdown and explicit Subscribe / Unsubscribe controls, sharing the connected MQTT client with publishing. Support one active subscription filter at a time. Changing the filter requires unsubscribing first; keep publishing available while subscribed.
- Use MQTT 3.1.1 subscriptions requesting QoS 0. Show pending, active, rejected, and unsubscribed states based on broker acknowledgements; do not claim a subscription is active before SUBACK. Show broker-granted QoS and acknowledgement errors in Output.
- Show each received message's actual topic, local arrival time, payload byte count, delivered QoS, and retained flag. Liminal's `mqtt_pub` publishes the message payload itself, without the TCP envelope; preserve that distinction in the viewer.
- Topics come from Liminal's configured `topic_map` or `default_topic`; Desk must not assume a universal output topic. To observe processed output, the selected filter must match the configured output topic.
- A broad filter may also receive Desk's own publishes or messages from other producers. Show the actual topic and do not label every received message as Liminal output or a reply to a particular send.
- On broker disconnection, clear the active subscription state. After reconnecting manually, require an explicit Subscribe action; do not automatically resubscribe or republish.

### Editable topic suggestions — version 1.1

- Use separate editable dropdowns for **Publish topic** and **Subscribe filter**. Users can select a built-in suggestion or enter any valid topic/filter; suggestions are conveniences, not restrictions.
- The publish dropdown initially offers and selects `liminal/sensors/esp32-001/imu`.
- The subscribe dropdown offers `liminal/#` and `liminal/sensors/esp32-001/imu`. Initially select `liminal/#` as a broad inspection filter, without subscribing automatically. It is not a claim that Liminal publishes to every matching topic.
- Add output-topic suggestions only after checking the relevant Liminal configuration, and identify their source. The initial built-in list need not contain an assumed output topic.
- Validate publish topics and subscription filters separately. Publish topics reject `+` and `#`; filters support `+` only as a complete topic level and `#` only as a complete final level. Both reject empty strings, invalid Unicode, null characters, and names exceeding MQTT's UTF-8 length limit. Preserve case and meaningful spaces.
- Reading suggestions automatically from a selected Liminal configuration and saving recent topics are deferred.

### Received-message view — version 1.1

- Each protocol tab has its own received-message list and a read-only detail viewer. Show local arrival time, peer/topic, and payload byte count in the list; selecting a message opens its contents.
- Display valid JSON with syntax highlighting and an optional formatted view. Preserve the original received text/bytes for copying; formatting must not alter the stored message or numeric literals. Received content must not replace the outgoing editor's contents.
- Show malformed JSON as received text with an explanatory diagnostic. For invalid UTF-8, provide a hexadecimal representation. An invalid payload must not crash reception or silently disappear.
- Provide Clear Messages, Copy Raw, and Copy Formatted JSON actions; formatted copying is available only for valid JSON. Do not automatically send, publish, or forward a received message.
- Bound each tab's history to 500 messages and 10 MiB of stored raw payload, evicting the oldest entries when either limit is reached. Default the per-message receive limit to 1 MiB; reject larger TCP frames as described above, and skip oversized MQTT messages with a diagnostic. Keep these limits visible/documented and reception responsive.
- Clearing history does not disconnect or unsubscribe. Switching tabs preserves history and the selected message. Stopping reception preserves already received messages; quitting discards in-memory history.

Output protocol basis: `src/processors/output/mqtt.rs` and `src/processors/output/tcp.rs`, inspected on 2026-11-04.

Default MQTT payload (unchanged by version 1.1):

```json
{
  "sensor_name": "main_imu",
  "sensor_type": "imu",
  "imu_type": "MPU6500",
  "timestamp": 1793770000000,
  "device_id": "esp32-001",
  "accelerometer": {
    "x": 0.196,
    "y": -0.412,
    "z": 9.806,
    "unit": "g"
  },
  "gyroscope": {
    "x": 0.512,
    "y": -0.128,
    "z": 9.124,
    "unit": "°/s"
  },
  "temperature": 26.4,
  "temperature_unit": "°C"
}
```

## 5. Version 1.2 — clean workspace and trace replay

This section records the agreed version 1.2 requirements, subsequently authorised and implemented. Existing transport defaults, framing, payloads, topic suggestions, receiving limits, and connection behaviour remain as specified above unless explicitly revised here.

### Native implementation and layout

- Continue using Python/PySide6 widgets. No Electron, embedded browser, Qt WebEngine, or HTML/JavaScript frontend is required for the application. The conversational status-bar preview is a design reference, not a runtime dependency or additional application feature.
- Retain the two top-level TCP/MQTT tabs and clear **Send** and **Receive** areas. Align headings, fields, toolbars, and spacing consistently; prioritise the outgoing editor and incoming message list/detail viewer.
- Keep disconnected endpoint fields editable. Once connected/listening, present a compact, readable connection summary with the relevant Disconnect / Stop Listening action and a way to reveal settings. Avoid displaying the summary as faint disabled form fields. Endpoint settings remain locked while their operation is active.
- Move occasional settings such as send/receive byte limits, credentials, and history-limit explanations into an expandable settings area. Values remain discoverable and documented. Do not add configurable history retention if it is not otherwise required by this contract.
- Keep one formatting action and one primary Send / Publish action in Message mode. In Trace mode, replace the primary send action with replay controls. Secondary actions may use small icons with accessible names and tooltips; essential actions retain text labels.
- Give the received-message list and JSON detail viewer usable space with clearly draggable dividers. Short windows must scroll or reflow without overlapping controls or hiding the detail viewer permanently. Place compact metadata and Copy actions next to the selected message.
- Add **Follow latest** to each received-message view. It is initially off, preserving manual inspection. When enabled, select and reveal the latest arrival; manually choosing an older message disables it. With Follow latest off, preserve the current selection while it remains in bounded history. The first arrival may be selected when no message has yet been selected. Clearly handle eviction of a selected message without showing stale contents.
- Remember window size, divider positions, and per-tab diagnostic-panel visibility across restarts. Restore geometry within available screens. This is UI-preference persistence only; credentials, endpoints, payloads, topics, traces, and received-message history are not automatically saved or restored.

### Persistent status bar

- Add a thin, native-style status bar that follows the selected protocol tab. A tab switch changes the visible indicators without stopping or altering background operations.
- TCP shows independent **Sender** and **Listener** indicators. MQTT shows independent **Broker** and **Subscription** indicators for its single shared broker connection. Both show trace state, a Problems counter, and an Output toggle.
- Use colour plus text/icon meaning: gray for disconnected, stopped, or inactive; amber for connection/subscription operations in progress; green for healthy connected, listening, or subscribed states; red for a current operation error. Colour must not be the only indication.
- Distinguish a healthy TCP listener waiting for a producer from one with an accepted peer. Example summaries: `Listener :10000 · Waiting`, `Listener :10000 · 1 peer`, and `Listener · Stopped`. Listening is a healthy green state even when no producer is connected.
- TCP sender states include disconnected, connecting, connected, and connection/send failure. TCP listener failures include bind/listen failure. A producer disconnect or invalid frame may create a diagnostic while a functioning listener continues to show Listening / Waiting.
- MQTT broker states include disconnected, connecting, connected, and broker/connection failure. Subscription states include inactive, awaiting SUBACK, active, awaiting UNSUBACK, and rejected/failed. Do not infer an active subscription from a connected broker.
- A subscription failure must leave a healthy broker indicator green; a sender failure must leave a healthy TCP listener green. Invalid outgoing JSON or a publish-topic validation error belongs to its operation/Problems entry and must not falsely mark a working broker connection as failed.
- Publishing needs no second MQTT connection or persistent connected indicator. Publishing availability follows the broker connection; a brief transmit/receive activity indication can accompany otherwise stable connection colours. Activity is not an acknowledgement that Liminal processed an event.
- Hover/focus reveals full destination, bind address, accepted peer, active filter, and relevant error details. Clicking an indicator reveals its operation controls/details or its diagnostic. These disclosures must remain usable with keyboard navigation.
- Surface concise error identifiers alongside readable explanations, with full details available on demand. Preserve actual backend/protocol codes where available. Use documented stable application identifiers where no useful code exists, such as `TCP_CONNECT_REFUSED`, `TCP_FRAME_TOO_LARGE`, `MQTT_SUB_REJECTED`, `JSON_INVALID`, and `TRACE_INVALID`; do not invent a broker-supplied code.
- A current error stays discoverable until corrected, superseded by the next attempt/result, or dismissed as a diagnostic. Dismissing a diagnostic must not change the actual connection/subscription state. A retry may show amber while pending, followed by the real result.
- Show a small red badge on an inactive protocol tab when it has an unresolved diagnostic. The status bar's Problems count covers the selected tab; tab badges expose errors elsewhere. Switching tabs alone must not clear errors.
- Proposed compact summaries follow the reviewed preview: TCP `Sender :9999 | Listener :10000 · 1 peer`; MQTT `Broker :1883 | Subscribed · factory/#`. `factory/#` illustrates a user-entered filter, not a change to the existing default `liminal/#`.

### Resizable Problems / Output panel

- Keep a diagnostic panel above the status bar with **Problems** and **Output** tabs. Drag its top divider to resize it; close it with a small close button or toggle it from the status bar. The status bar remains available while the panel is hidden.
- Clicking the Problems counter reveals the Problems tab. Clicking Output reveals/toggles Output. Remove the standalone Hide Panel / Show Panel buttons from the main workspace.
- Put Copy Output and Clear Output inside the diagnostic panel toolbar. Retain the bounded 500-line Output log for each protocol tab and preserve logs while switching tabs or hiding the panel.
- Problems includes outgoing JSON/trace diagnostics and current operation failures, with useful identifiers and locations where applicable. Clicking an entry navigates to the editor location or relevant connection details. Resolve stale editor diagnostics after correction; do not erase unrelated network errors when JSON becomes valid.
- New errors update the counter and protocol-tab badge without repeatedly reopening a panel the user closed. A failed explicit Send or Run must provide visible feedback through inline diagnostics/status even if the panel stays closed.
- Output remains a read-only event log. Received payloads remain in the separate received-message viewer. Clearing Output must not change connections, erase message history, or silently resolve Problems.

### Message and Trace modes

- Add an explicit **Message / Trace** selector in each protocol's sending area. Message mode preserves the existing single-JSON behaviour, including the ability to send a JSON array or an object containing fields named `type`, `payload`, or `duration_ms` as ordinary data.
- Trace mode interprets a versioned JSON document containing an ordered list of send/delay steps. Maintain separate in-memory editor contents for the two modes so switching modes does not overwrite a single message or trace. Switching protocol tabs also preserves both editors and replay state.
- Use the same highlighting, formatting, and VS Code-style diagnostics for trace editing. Add semantic validation for the trace schema and step fields, with line/column or step-index locations as appropriate.
- The initial schema is the following. Version `1` is the trace-file format version, independent of the application's version 1.2:

```json
{
  "version": 1,
  "steps": [
    {
      "type": "send",
      "payload": {
        "device_id": "esp32-001",
        "event": "started"
      }
    },
    {
      "type": "delay",
      "duration_ms": 1000
    },
    {
      "type": "send",
      "payload": {
        "device_id": "esp32-001",
        "event": "stopped"
      }
    }
  ]
}
```

- Require a top-level object with exactly `version` and `steps`; support version `1` and a non-empty array of steps. A send step contains exactly `type: "send"` and a required `payload` of any valid JSON type, including null. A delay step contains exactly `type: "delay"` and a required non-negative integer `duration_ms`; booleans are not integers for this purpose. Reject unknown versions, step types, and unsupported/extra control fields with a useful diagnostic.
- Treat each send payload as the outgoing application data. Preserve its JSON values, numeric literals, string escapes, object-key ordering, and duplicate keys when extracting it from the trace. Desk may normalise structural whitespace, but must not round numbers or alter their meaning.
- Delay steps are executed inside Desk and never transmitted to Liminal. Trace-control wrappers are never sent. For TCP, encode each extracted payload with the existing four-byte byte-length prefix; for MQTT, publish only that payload as UTF-8 JSON.
- Each trace uses the sending endpoint and, for MQTT, publish topic selected for that protocol. Per-step topic/endpoint overrides are outside this version.

### Replay execution

- Provide **Run**, **Pause / Resume**, and **Stop** with a compact progress display, such as `Step 2 of 3 — waiting, 650 ms remaining`. Reflect replay state in the selected tab's status bar and preserve it while switching tabs.
- Validate the entire trace, payload sizes, and the selected endpoint/topic before any network transmission. Require an established persistent TCP sending connection or an accepted MQTT broker connection before Run. Trace mode requires Keep connection open for TCP; one-shot connection-per-step replay is deferred.
- Freeze the trace contents, sending endpoint, publish topic, and relevant transport settings for the run. Disable edits to those values and switching Message / Trace mode while running or paused. Keep incoming-message inspection, protocol-tab switching, diagnostic-panel controls, and Stop available.
- Support one replay per protocol tab, allowing TCP and MQTT replays to operate independently. Never permit overlapping runs or manual single-message sends on a sender that is replaying. Starting/stopping one trace must not alter the other protocol's operations.
- Process steps in order and wait for each local transmission completion before advancing. Start a delay after completion of the preceding step, including any preceding delay. Local completion does not establish delivery to or processing by Liminal; do not wait for received output as an acknowledgement.
- Use responsive, non-blocking timers. Measure elapsed waits with a monotonic clock; system clock adjustments must not change replay intervals. Delays are minimum local waits, subject to normal scheduling/transmission latency, not a real-time timing guarantee.
- Pausing a delay retains its remaining duration; Resume waits for that remainder. Pausing during an in-flight send lets that send finish, then pauses before the next step. Do not duplicate the completed send on Resume. Make Pausing / Paused feedback explicit.
- Stop cancels remaining steps and waits. It cannot retract a transmission already in flight or completed; report any in-flight step accurately and do not begin another step. Stopping a trace preserves its editor, received messages, and otherwise healthy connections. Stop must also work while paused.
- An unexpected sending-connection loss or send failure stops that trace, including during pauses/delays, and leaves an error plus the failing/current step discoverable. Never automatically retry, reconnect, resume, or replay a payload. A user can explicitly Run again from the first step after correcting the failure; restarting is not an implicit continuation.
- Disconnecting the sending client or closing the app stops its trace and cancels timers/background work. Stopping the independent TCP listener or unsubscribing MQTT reception does not stop a healthy sender's trace. Disconnecting the shared MQTT broker necessarily stops MQTT replay and clears its subscription.
- Keep receiving and message inspection active throughout replay. Output records starts, pauses/resumes, step sends/waits, completion, stopping, and failures without treating received messages as correlated replies.

### Trace files

- Add Open Trace and Save Trace actions using explicit file dialogs and UTF-8 `.json` files. Save the trace document only; do not embed credentials or endpoint profiles. File actions do not run the trace automatically.
- Loading preserves the previous editor if the file cannot be read/decoded. A readable but invalid trace may be loaded for correction, with diagnostics and Run unavailable until valid. Ask before replacing unsaved trace edits; warn before closing the app with unsaved trace edits. No automatic payload/trace persistence or recovery files are introduced.
- Save the editor's text explicitly, including an invalid draft if desired; label it as invalid and keep validation separate from saving. Successful saving clears the unsaved indicator; failed saving preserves the editor and unsaved state. Confirm overwriting an existing file using the native save dialog.

## 6. Scope boundaries

Version 1 supports one TCP connection and one MQTT connection, which may remain open independently while switching tabs. Editing endpoint settings requires disconnection. Preserve editor contents and settings while switching tabs; automatic persistence of endpoints and content across application restarts is deferred. Version 1.2 adds UI-preference persistence only, as specified in section 5.

Version 1.1 adds one TCP listener with one accepted producer and one MQTT subscription on the existing broker connection, plus independent received-message histories. All may remain active while switching tabs. Listener settings can be edited only when stopped; broker endpoint settings still require disconnection. Closing the app releases clients, accepted sockets, the listener, subscriptions, and background work.

Version 1.2 adds explicit outgoing trace replay and trace-file open/save as described in section 5. It does not replay received-message history. The following remain outside version 1.2:

- Multiple simultaneous TCP producers/listeners, multiple MQTT brokers or subscription filters, and TCP receive-client mode.
- Automatic request/reply correlation, delivery acknowledgements from Liminal, or replay/forwarding of received messages.
- HTTP/REST support, TLS/certificates, and MQTT over WebSockets.
- Liminal configuration editing, process control, or changes to Liminal itself.
- Payload templates, schema-specific forms, saved connection profiles, received-message import/export, and received-message history on disk. Explicit trace-file open/save is included in version 1.2.
- Wall-clock scheduled sending, looping, playback-speed controls, batch/load testing, automatic retries, per-step topic/endpoint overrides, and expected-output assertions. Finite sequential trace replay with explicit delay steps is included in version 1.2.
- A signed installer or App Store distribution. Initially provide documented local setup and launch instructions for macOS.

## 7. Implementation plan

Version 1.1 implementation followed these steps:

1. Extend the existing two tabs with independent TCP send/listen controls and shared MQTT publish/subscribe controls.
2. Add editable topic suggestions and distinct topic/filter validation.
3. Implement buffered TCP frame reception and acknowledged MQTT subscription handling.
4. Add bounded message histories and read-only JSON/raw viewers, preserving the existing outgoing editors and diagnostic panel.
5. Test fragmentation, multiple frames, malformed/oversized payloads, subscription errors, simultaneous send/receive, and cleanup. Verify both output paths through a temporary Liminal pipeline and update the usage instructions.

Version 1.2 implementation followed this plan:

1. Refactor connection summaries and settings, then introduce native per-protocol status indicators and the persistent status-bar panel controls.
2. Rework panel sizing/visibility, received-message space, Follow latest behaviour, and UI-preference persistence. Preserve independent connection/subscription state and current sender/receiver behaviour.
3. Implement trace parsing/semantic diagnostics and explicit Open/Save Trace, preserving payload numeric literals and editor contents.
4. Implement per-protocol replay state machines with non-blocking timing, local-send completion, pause/resume/stop, immutable run settings, and failure handling.
5. Verify the acceptance criteria below, inspect native light/dark and compact-window layouts, exercise both transports through Liminal with temporary configuration, and update the README.

The Python application lives in the separate authorised project directory `/Users/duncan/Dropbox/Postdoc/Development/liminal-desk`. Dependencies are declared in `requirements.txt` and installed in the local `venv` directory. The Liminal source and configuration are not changed by this implementation.

## 8. Acceptance criteria

Version 1 sending acceptance criteria (retained):

1. Invalid addresses, ports, topics, or JSON produce useful errors without transmission. Both JSON editors highlight all JSON token types and provide VS Code-style JSON diagnostics: a red squiggly underline, a red gutter marker, an explanatory hover tooltip, and a clickable Problems entry with line/column numbers that navigates to the error. End-of-payload errors have a visible insertion-point marker. Diagnostics clear after correction, without modal JSON error dialogs. Failed formatting preserves the editor contents.
2. A TCP receiver observes exactly the four-byte big-endian byte count and the corresponding UTF-8 JSON, including non-ASCII examples.
3. Multiple sends reuse one TCP connection in persistent mode; one-shot sends close their connection after transmission.
4. An MQTT subscriber receives the JSON on the chosen topic, using QoS 0 and Retain off, without the TCP prefix.
5. Connection refusal, timeout, connection loss, and oversized TCP payloads produce clear feedback without freezing the window or replaying a message.
6. Both tabs can stay connected independently; switching tabs preserves their contents, and closing the application releases both connections.
7. A manual smoke check against Liminal confirms that valid TCP and MQTT messages reach the configured pipeline. The interface distinguishes transmission from application processing.

Version 1.1 receiving and topic-selection acceptance criteria:

1. The existing TCP/MQTT defaults, payload editors, sending behaviour, JSON diagnostics, and diagnostic panels continue to work.
2. A Liminal `tcp_output` in client mode can connect to Desk's listener and send multiple envelope messages over one persistent connection while Desk's sending client remains connected independently.
3. TCP reception correctly handles headers and bodies split across reads, several frames in one read, Unicode, oversized lengths, partial-frame disconnects, peer reconnection, and listener bind failures without freezing or losing frame boundaries.
4. Desk subscribes/unsubscribes on the shared MQTT connection, reflects broker acknowledgements/rejections, receives matching messages with their actual topics and metadata, and continues publishing while subscribed. Disconnecting clears subscription state and does not trigger automatic replay or resubscription.
5. Both topic dropdowns allow selection and custom input. Publish wildcard rejection and valid/invalid subscription filter cases are checked independently. Output suggestions are not presented as verified without a checked configuration source.
6. Message selection, JSON highlighting, raw/formatted copying, invalid JSON/UTF-8 handling, size/history limits, and clearing work independently in both tabs. Incoming messages never overwrite outgoing payloads.
7. Both receive paths are verified against Liminal output with temporary configuration, including the TCP envelope versus MQTT payload-only distinction. A broad MQTT subscription does not imply source identification or request/reply correlation.
8. Switching tabs preserves active operations and message views; stopping one operation leaves the others running. Closing the app releases all networking resources and background work.

Version 1.2 interface and trace acceptance criteria:

1. The status bar switches between independent TCP Sender/Listener and MQTT Broker/Subscription summaries. Connection transitions, listener waiting/peer states, pending/rejected subscriptions, and errors have readable text plus the agreed colours. Failure of one operation does not falsely change a healthy independent indicator.
2. Clicking/hovering indicators exposes full details and useful error identifiers/backend codes. Problems counts and inactive-tab error badges remain accurate; correcting JSON does not clear unrelated network diagnostics.
3. Problems/Output can be opened from the status bar, resized with the divider, and closed without losing logs or connections. New errors stay discoverable without forcing a dismissed panel open. Copy/Clear actions work inside the panel, and clearing Output does not clear message history or change actual operation state.
4. Connected summaries are readable, settings remain discoverable, and all controls/detail viewers remain usable at supported window sizes in native light/dark appearance. Follow latest selects arrivals only when enabled; manual inspection remains stable. Window/divider/panel preferences restore without saving secrets or message content.
5. Message mode still sends ordinary JSON arrays and objects containing trace-like keys. Switching Message/Trace mode preserves separate editor contents and does not accidentally execute or transmit trace control data.
6. Entire traces are validated before sending. Tests cover unsupported versions/steps/fields, missing/null payloads, malformed JSON, invalid delays (including negative, fractional, and boolean values), oversized payloads, and invalid selected endpoints/topics. No prefix of an invalid trace is transmitted.
7. Independent wire peers observe only the expected payloads, in order, with existing TCP framing or MQTT QoS 0 / Retain off. Extracted numeric literals, escapes, ordering, and duplicate keys are preserved. Delay/control objects never appear on the wire.
8. Timed tests verify minimum waits after preceding local completions, zero/adjacent delays, pause during a delay, pause during an in-flight send, resume without duplicate transmission, and Stop during running/paused/waiting states. The window and receivers remain responsive.
9. One trace per protocol can run independently. Run settings/mode are locked while active, manual sends cannot interleave, switching protocol tabs preserves progress, and stopping reception does not stop an independent sender. Broker disconnection, sending failure, and app close stop the appropriate replay and release timers/work without automatic retries or continuation.
10. Trace files open/save through explicit dialogs. Read/decode/write failures preserve edits; readable invalid drafts get diagnostics; unsaved replacement/close and overwrite confirmations work. File operations never start replay, and payloads/credentials are not automatically persisted.
11. TCP and MQTT traces are exercised through a temporary Liminal pipeline while Desk receives output. Tests distinguish local transmission from application processing; no request/reply correlation or real-time timing guarantee is implied. Existing version 1/1.1 regression checks continue to pass.

## 9. Agreement

The user approved implementation of the version 1 sending scope on 2026-11-04 and authorised read/write access to the separate `liminal-desk` directory. Python/PySide6 and the program name Liminal Desk are agreed. Later scope changes should be recorded here.

On 2026-11-04 the user agreed the receiving/topic-dropdown design, requested this contract update, and subsequently authorised implementation. Version 1.1 is implemented with verification documented in README.md.

On 2026-11-04 the user requested a further requirements update after reviewing the native status-bar direction and discussing trace replay. Section 5 records version 1.2 for review. The user subsequently explicitly authorised building version 1.2. The implementation and acceptance checks are complete; the README records validation and usage.
