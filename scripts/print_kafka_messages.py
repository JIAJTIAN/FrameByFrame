"""Print raw Kafka messages for Bluesky/beamline connection testing.

This diagnostic script only reads and prints. It does not reduce data and does
not write analysis files. Use it on the beamline server to discover the exact
payload format before connecting messages to the v3 reduction queue.
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print live raw Kafka messages from a topic.")
    parser.add_argument("--bootstrap-servers", default="164.54.169.92:9092")
    parser.add_argument("--topic", default="bluesky_aswaxs")
    parser.add_argument("--group-id", default="aswaxs-v3-debug-printer")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Stop if no message arrives in this time.")
    parser.add_argument("--max-messages", type=int, default=0, help="0 means run until timeout/interrupted.")
    parser.add_argument("--max-chars", type=int, default=4000, help="Maximum characters printed per message.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        from kafka import KafkaConsumer
    except ImportError as exc:
        raise RuntimeError("Install kafka-python first: pip install kafka-python") from exc

    consumer = KafkaConsumer(
        args.topic,
        bootstrap_servers=args.bootstrap_servers.split(","),
        group_id=args.group_id,
        auto_offset_reset="latest",
        enable_auto_commit=False,
        consumer_timeout_ms=args.timeout_ms,
        value_deserializer=lambda value: value,
    )

    print(f"Listening to Kafka bootstrap={args.bootstrap_servers} topic={args.topic}")
    print(f"Timeout without messages: {args.timeout_ms} ms")
    count = 0
    try:
        for message in consumer:
            count += 1
            print(
                f"\nmessage {count}: topic={message.topic} partition={message.partition} "
                f"offset={message.offset} key={decode_value(message.key)!r}"
            )
            value = decode_value(message.value)
            print_limited(value, args.max_chars)
            if args.max_messages and count >= args.max_messages:
                break
    finally:
        consumer.close()

    if count == 0:
        print("No Kafka messages received before timeout.")
        return 1
    return 0


def decode_value(value: bytes | None) -> Any:
    if value is None:
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary_hex": value[:120].hex(), "bytes": len(value)}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def print_limited(value: Any, max_chars: int) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + " ... <truncated>"
    print(text)


if __name__ == "__main__":
    raise SystemExit(main())
