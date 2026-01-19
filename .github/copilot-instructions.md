# KUKSA-MBS Bridge Coding Guidelines

## Architecture Overview
This codebase implements a UDP bridge between KUKSA Databroker (automotive data service) and MBSE tools like Modelica. It translates VSS signals to/from UDP packets for simulation integration.

**Key Components:**
- `udp_databroker_provider_v3.py`: Main config-driven provider supporting multiple signals
- `udp_databroker_provider.py`: Legacy single-signal command-line version
- `actuate.py`: Manual actuation testing script
- `test*.py`: Validation scripts using kuksa-client SDK
- `modelica-mock/`: UDP relay for testing without real Modelica

**Data Flow:**
- **Ingress (MBSE → KUKSA):** UDP listener receives sensor data, publishes as CURRENT values
- **Egress (KUKSA → MBSE):** Subscribes to TARGET values, sends actuation commands via UDP
- **Actuation:** Registers as provider for actuator signals, handles actuation requests

## Essential Patterns

### Signal Configuration
Use YAML config with signal definitions including path, dtype, and UDP endpoints:

```yaml
signals:
  - path: Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position
    dtype: uint8  # uint8/bool/float32le/uint32/text
    udp_listen: "0.0.0.0:50000"  # Receive from MBSE
    udp_send: "127.0.0.1:50001"  # Send to MBSE
    provide_actuation: true  # Enable actuation handling
```

### UDP Encoding/Decoding
Follow dtype-specific encoding in `encode_value()`/`decode_value()`:
- `uint8`: Single byte 0-255
- `bool`: Single byte 0x00/0x01
- `float32le`: Little-endian 4-byte float
- `text`: UTF-8 string with newline

### Async Patterns
Use asyncio for all network operations. Create datagram endpoints with protocols:

```python
transport, _ = await loop.create_datagram_endpoint(
    lambda: ListenAndPublish(client, path, dtype),
    local_addr=(host, port)
)
```

### KUKSA Client Usage
Connect with VSSClient, use async methods:

```python
client = VSSClient(host, port, token=token, **tls_kwargs)
await client.connect()
await client.set_current_values({path: Datapoint(value)})
```

Subscribe with SubscribeEntry for TARGET values:

```python
entries = [SubscribeEntry(path=path, view=View.TARGET_VALUE, fields=[Field.VALUE])]
async for updates in client.subscribe(entries=entries):
    # Process updates
```

### Actuation Provider
For actuators, use OpenProviderStream to register and handle requests:

```python
stream = stub.OpenProviderStream()
await stream.write(vpb.OpenProviderStreamRequest(
    provide_actuation_request=vpb.ProvideActuationRequest(
        actuator_identifiers=[tpb.SignalID(path=path)]
    )
))
```

## Development Workflow

### Running the Provider
```bash
# V3 with config
python udp_databroker_provider_v3.py config.yaml

# V1 with args
python udp_databroker_provider.py --broker-host localhost --broker-port 55555 \
  --angle-path Vehicle.Custom.Door.Angle --command-path Vehicle.Body.Door.IsOpen \
  --udp-angle-listen 0.0.0.0:50000 --udp-command-send 127.0.0.1:50001
```

### Testing
```bash
# Get current value
grpcurl -plaintext -d '{"signalId":{"path":"Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position"}}' \
  localhost:55555 kuksa.val.v2.VAL/GetValue

# Mock MBSE relay
python modelica-mock/udp_modelica_placeholder.py --listen 0.0.0.0 --port 50001

# Manual actuation
python actuate.py --path Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position --type uint32 --value 50
```

### Dependencies
- `kuksa-client==0.5.0` (async gRPC client)
- `pyyaml` (for config files)
- Python 3.9+ for asyncio features

## Conventions
- Signal paths follow VSS standard (e.g., `Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position`)
- UDP ports: 50000+ for sensors, 50001+ for actuators
- Logging: Use `logging.info()` for key events, `logging.debug()` for data flow
- Error handling: Catch exceptions in async tasks, log warnings for invalid data
- TLS: Optional, configured via broker.tls in config.yaml</content>
<parameter name="filePath">.github/copilot-instructions.md