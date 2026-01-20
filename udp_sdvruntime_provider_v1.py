from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kuksa_client.grpc.aio import VSSClient, Datapoint
from kuksa_client.grpc import SubscribeEntry, View, Field

# ----------------------
# Config
# ----------------------

def load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config file not found: {p}")
    if p.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # pip install pyyaml
        except ImportError:
            raise SystemExit("YAML config requested but PyYAML not installed. `pip install pyyaml`")
        with p.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def parse_hostport(value: str) -> Tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)

# ----------------------
# UDP value codec
# ----------------------

def encode_value(dtype: str, value: Any) -> bytes:
    """
    Encode python value -> UDP payload bytes.
    Supported dtype:
      - float32le, uint32, uint8, bool, text, float_text
    """
    if dtype == "float32le":
        return struct.pack("<f", float(value))
    if dtype == "uint32":
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if dtype == "uint8":
        return bytes([max(0, min(255, int(value)))])
    if dtype == "bool":
        return b"\x01" if bool(value) else b"\x00"
    if dtype == "text":
        return (str(value) + "\n").encode("utf-8")
    if dtype == "float_text":
        return (f"{float(value)}\n").encode("utf-8")
    raise ValueError(f"Unsupported dtype for encode: {dtype}")

def decode_value(dtype: str, data: bytes) -> Any:
    """
    Decode UDP payload bytes -> python value.
    Supported dtype:
      - float32le, uint32, uint8, bool, text, float_text
    """
    if dtype == "float32le":
        if len(data) < 4:
            raise ValueError("need 4 bytes")
        return struct.unpack("<f", data[:4])[0]
    if dtype == "uint32":
        if len(data) < 4:
            raise ValueError("need 4 bytes")
        return struct.unpack("<I", data[:4])[0]
    if dtype == "uint8":
        if len(data) < 1:
            raise ValueError("need 1 byte")
        return data[0]
    if dtype == "bool":
        if len(data) < 1:
            raise ValueError("need 1 byte")
        return bool(data[0])
    if dtype == "text":
        # Return string as-is (symmetry with encode_value("text"))
        return data.decode("utf-8", errors="replace").strip()
    if dtype == "float_text":
        return float(data.decode("utf-8", errors="replace").strip())
    raise ValueError(f"Unsupported dtype for decode: {dtype}")

# ----------------------
# UDP sender (simple, per-send socket)
# ----------------------

async def udp_send(dst: Tuple[str, int], payload: bytes) -> None:
    """
    Simple UDP send helper.
    Note: Creates a transport per send (fine for low rate; optimize if needed).
    """
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: asyncio.DatagramProtocol(),
        remote_addr=dst,
    )
    try:
        transport.sendto(payload)
    finally:
        transport.close()

# ----------------------
# UDP ingress -> publish CURRENT to Databroker
# ----------------------

class ListenAndPublish(asyncio.DatagramProtocol):
    def __init__(self, client: VSSClient, path: str, dtype: str):
        self.client = client
        self.path = path
        self.dtype = dtype
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = self.transport.get_extra_info("sockname")
        logging.info("Listening UDP for %s on %s (dtype=%s)", self.path, sock, self.dtype)

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.create_task(self._handle(data, addr))

    async def _handle(self, data: bytes, addr) -> None:
        try:
            value = decode_value(self.dtype, data)
        except Exception as e:
            logging.warning("Decode failed for %s from %s: %s", self.path, addr, e)
            return

        try:
            await self.client.set_current_values({self.path: Datapoint(value)})
            logging.debug("Published CURRENT %s = %r", self.path, value)
        except Exception as e:
            logging.exception("Publish CURRENT failed for %s: %s", self.path, e)

# ----------------------
# Subscribe TARGET -> forward to UDP
# ----------------------

async def subscribe_targets(client: VSSClient, signals: List[Dict[str, Any]]) -> None:
    """
    Subscribe to TARGET values for signals having udp_send and forward updates to UDP.
    """
 

    entries = []
    by_path: Dict[str, Dict[str, Any]] = {}

    for s in signals:
        if s.get("udp_send"):
            entries.append(
                SubscribeEntry(
                    path=s["path"],
                    view=View.TARGET_VALUE,
                    fields=[Field.VALUE],
                )
            )
            by_path[s["path"]] = s

    if not entries:
        logging.info("No TARGET subscriptions configured.")
        # keep task alive (optional), or return
        while True:
            await asyncio.sleep(3600)

    logging.info("Subscribing to %d TARGET signals", len(entries))

    # kuksa_client versions differ: some have subscribe_v2, some have subscribe.
    subscribe_v2 = getattr(client, "subscribe_v2", None)
    aiter = subscribe_v2(entries=entries) if callable(subscribe_v2) else client.subscribe(entries=entries)

    async for batch in aiter:
        # Normalize updates list across versions
        updates_iter = getattr(batch, "updates", None)
        if updates_iter is None:
            updates_iter = batch if isinstance(batch, (list, tuple)) else [batch]

        for u in updates_iter:
            # Different SDK versions store path/datapoint slightly differently
            path = getattr(u, "path", None) or getattr(getattr(u, "entry", None), "path", None)
            dp = getattr(u, "datapoint", None) or getattr(getattr(u, "entry", None), "datapoint", None)
            val = getattr(dp, "value", None) if dp is not None else None

            if not path or path not in by_path or val is None:
                continue

            s = by_path[path]
            try:
                payload = encode_value(s["dtype"], val)
            except Exception as e:
                logging.warning("Encode failed for %s (val=%r, dtype=%s): %s", path, val, s.get("dtype"), e)
                continue

            dst = parse_hostport(s["udp_send"])
            await udp_send(dst, payload)
            logging.info("TARGET → UDP %s = %r -> %s", path, val, s["udp_send"])

# ----------------------
# Main
# ----------------------

async def run(config_path: str) -> None:
    cfg = load_config(config_path)

    broker = cfg["broker"]
    host, port = broker["host"], int(broker["port"])
    token = broker.get("token") or None

    tls_kwargs = {}
    tls = broker.get("tls", {}) or {}
    if tls.get("enabled"):
        ca = tls.get("root_ca")
        if not ca:
            raise SystemExit("TLS enabled but no root_ca provided")
        tls_kwargs = {
            "root_certificates": Path(ca),
            "tls_server_name": tls.get("server_name", host),
        }

    client = VSSClient(host, port, token=token, **tls_kwargs)
    await client.connect()
    logging.info("Connected to Databroker at %s:%d", host, port)

    signals = cfg.get("signals", [])

    # 1) UDP listeners: ingress -> CURRENT
    loop = asyncio.get_running_loop()
    listeners = 0
    for s in signals:
        if s.get("udp_listen"):
            h, p = parse_hostport(s["udp_listen"])
            await loop.create_datagram_endpoint(
                lambda s=s: ListenAndPublish(client, s["path"], s["dtype"]),
                local_addr=(h, p),
            )
            listeners += 1
    logging.info("Started %d UDP listeners (UDP -> CURRENT)", listeners)

    # 2) TARGET subscriptions: egress -> UDP
    sub_task = asyncio.create_task(subscribe_targets(client, signals))

    try:
        await sub_task
    finally:
        await client.disconnect()
        logging.info("Disconnected.")

def main() -> None:
    ap = argparse.ArgumentParser(description="UDP ⇄ KUKSA Databroker Bridge (v1 TARGET/CURRENT only)")
    ap.add_argument("config", help="Path to config (YAML or JSON)")
    ap.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(message)s",
    )

    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
