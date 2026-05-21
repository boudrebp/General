#!/usr/bin/env python3
"""Bulk sender for MeterException payloads.

Reads a template JSON file and a text list of events, then POSTs one payload per line.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse
from urllib import request, error

NAME_TO_ID_MAP = {
    "primary power down": "18001",
    "primary power up": "18002",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send MeterException payloads to an API endpoint in bulk"
    )
    parser.add_argument(
        "--template",
        default="Sample MeterException Payload",
        help="Path to JSON template payload file",
    )
    parser.add_argument(
        "--events",
        required=True,
        help=(
            "Text file with one event per line in the format: "
            "meter_id,timestamp,name[,outage_id]"
        ),
    )
    parser.add_argument("--endpoint", required=True, help="Target API URL")
    parser.add_argument(
        "--token-url",
        help="OAuth token endpoint URL for bearer token retrieval",
    )
    parser.add_argument("--token-client-id", help="OAuth client_id")
    parser.add_argument("--token-client-secret", help="OAuth client_secret")
    parser.add_argument(
        "--token-project-id",
        help="Optional project ID used to replace {{projectID}} in --token-scope",
    )
    parser.add_argument(
        "--token-scope",
        help="Optional OAuth scope sent to token endpoint",
    )
    parser.add_argument(
        "--token-grant-type",
        default="client_credentials",
        help="OAuth grant_type for token request (default: client_credentials)",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Additional HTTP header in Key:Value format. Can be repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=0,
        help="Delay between requests in milliseconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads without sending requests",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately when one request fails",
    )
    return parser.parse_args()


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_headers(header_args: list[str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for item in header_args:
        if ":" not in item:
            raise ValueError(f"Invalid header format (expected Key:Value): {item}")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def load_template(path: str) -> dict[str, Any]:
    template_text = Path(path).read_text(encoding="utf-8")
    return json.loads(template_text)


def replace_tokens(value: Any, meter_id: str, iso_timestamp: str) -> Any:
    if isinstance(value, dict):
        return {k: replace_tokens(v, meter_id, iso_timestamp) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_tokens(item, meter_id, iso_timestamp) for item in value]
    if isinstance(value, str):
        return (
            value.replace("{{meter_id}}", meter_id)
            .replace("{{$isoTimestamp}}", iso_timestamp)
        )
    return value


def parse_event_line(line: str, line_num: int) -> dict[str, str | None]:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return {}

    delimiter = "|" if "|" in raw else ","
    parts = [part.strip() for part in raw.split(delimiter)]

    if len(parts) < 3:
        raise ValueError(
            f"Line {line_num}: expected meter_id,timestamp,name[,outage_id]"
        )

    meter_id, timestamp = parts[0], parts[1]
    if len(parts) == 3:
        name = parts[2]
        outage_id = None
    else:
        name = delimiter.join(parts[2:-1]).strip()
        outage_id = parts[-1] or None

    if not meter_id or not timestamp or not name:
        raise ValueError(
            f"Line {line_num}: meter_id, timestamp, and name are required"
        )

    return {
        "meter_id": meter_id,
        "timestamp": timestamp,
        "name": name,
        "outage_id": outage_id,
    }


def resolve_id_from_name(name: str) -> str:
    match = NAME_TO_ID_MAP.get(name.strip().lower())
    if match is None:
        raise ValueError(
            "Name must be either 'Primary Power Up' or 'Primary Power Down'"
        )
    return match


def build_payload(
    template_payload: dict[str, Any],
    meter_id: str,
    timestamp: str,
    name: str,
    outage_id: str | None,
) -> dict[str, Any]:
    payload = replace_tokens(deepcopy(template_payload), meter_id, timestamp)
    resolved_id = resolve_id_from_name(name)

    meter_exception = (
        payload["s:Envelope"]["s:Body"]["ExceptionsArrived"]["input"]
        ["MeterExceptionCollection"]["MeterException"][0]
    )

    meter_exception["Name"] = name
    meter_exception["ID"] = resolved_id

    if outage_id:
        meter_exception["Arguments"]["a:Argument"]["a:Value"] = outage_id

    return payload


def send_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def fetch_bearer_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str | None,
    grant_type: str,
    timeout: float,
) -> str:
    form_data: dict[str, str] = {
        "grant_type": grant_type,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        form_data["scope"] = scope

    body = parse.urlencode(form_data).encode("utf-8")
    req = request.Request(
        url=token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise ValueError(
            f"Token request failed with status {exc.code}: {response_text[:500]}"
        ) from exc

    try:
        token_json = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Token response is not valid JSON: {response_text[:500]}"
        ) from exc

    access_token = token_json.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Token response does not contain a valid access_token")
    return access_token


def main() -> int:
    args = parse_args()

    try:
        headers = parse_headers(args.header)
        template_payload = load_template(args.template)
        if args.token_url:
            if not args.token_client_id or not args.token_client_secret:
                raise ValueError(
                    "When --token-url is set, --token-client-id and "
                    "--token-client-secret are required"
                )
            scope = args.token_scope
            if scope and "{{projectID}}" in scope:
                if not args.token_project_id:
                    raise ValueError(
                        "--token-project-id is required when --token-scope "
                        "contains {{projectID}}"
                    )
                scope = scope.replace("{{projectID}}", args.token_project_id)
            token = fetch_bearer_token(
                token_url=args.token_url,
                client_id=args.token_client_id,
                client_secret=args.token_client_secret,
                scope=scope,
                grant_type=args.token_grant_type,
                timeout=args.timeout,
            )
            headers["Authorization"] = f"Bearer {token}"
            print("Token acquired successfully; using bearer authorization header")
    except Exception as exc:  # noqa: BLE001
        print(f"Setup error: {exc}", file=sys.stderr)
        return 2

    success = 0
    failed = 0

    with Path(args.events).open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            try:
                parsed = parse_event_line(line, line_num)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(str(exc), file=sys.stderr)
                if args.stop_on_error:
                    break
                continue

            if not parsed:
                continue

            meter_id = str(parsed["meter_id"])
            timestamp = str(parsed["timestamp"])
            name = str(parsed["name"])
            outage_id = parsed["outage_id"]

            # Allow NOW/now shortcut for per-line current UTC timestamp.
            if timestamp.lower() == "now":
                timestamp = now_iso_utc()

            try:
                payload = build_payload(
                    template_payload,
                    meter_id,
                    timestamp,
                    name,
                    outage_id,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"Line {line_num}: payload build error: {exc}", file=sys.stderr)
                if args.stop_on_error:
                    break
                continue

            if args.dry_run:
                print(
                    f"Line {line_num}: DRY RUN meter_id={meter_id} "
                    f"name={name}"
                )
                print(json.dumps(payload, indent=2))
                success += 1
            else:
                status, body = send_json(args.endpoint, payload, headers, args.timeout)
                if 200 <= status < 300:
                    success += 1
                    print(
                        f"Line {line_num}: sent meter_id={meter_id} "
                        f"name={name} status={status}"
                    )
                else:
                    failed += 1
                    print(
                        f"Line {line_num}: failed meter_id={meter_id} "
                        f"name={name} status={status}",
                        file=sys.stderr,
                    )
                    if body:
                        print(f"  response: {body[:500]}", file=sys.stderr)
                    if args.stop_on_error:
                        break

            if args.delay_ms > 0:
                time.sleep(args.delay_ms / 1000.0)

    print(f"Complete: success={success} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
