"""
Subscription Proxy with Balancer
Transforms multiple Xray configs into single config with load balancing
For use with Remnawave panel and Happ client
"""

import os
import json
import copy
import re
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="Subscription Proxy", version="1.0.0")

# Configuration from environment
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://127.0.0.1:3010")
BALANCER_NAME = os.getenv("BALANCER_NAME", "🇵🇱 Польша")
PROBE_URL = os.getenv("PROBE_URL", "https://www.google.com/generate_204")
PROBE_INTERVAL = os.getenv("PROBE_INTERVAL", "10s")
FORWARDED_HOST = os.getenv("FORWARDED_HOST", "subs.pavuka.cv")
DEFAULT_BALANCER_STRATEGY = os.getenv("DEFAULT_BALANCER_STRATEGY", "random")
GROUP_RULES_PATH = os.getenv("GROUP_RULES_PATH", "").strip()

PROXY_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}
IMPORTANT_HEADERS = [
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "profile-web-page-url",
    "support-url",
]

_group_rules_cache: dict[str, Any] = {
    "path": None,
    "mtime": None,
    "rules": [],
}


def _extract_subscription_headers(upstream_response: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header_name in IMPORTANT_HEADERS:
        if header_name in upstream_response.headers:
            headers[header_name] = upstream_response.headers[header_name]
    return headers


def _extract_proxy_outbound(config: dict) -> dict | None:
    for outbound in config.get("outbounds", []):
        if outbound.get("protocol") in PROXY_PROTOCOLS:
            return outbound
    return None


def _extract_outbound_address(outbound: dict | None) -> str:
    if not isinstance(outbound, dict):
        return ""

    settings = outbound.get("settings", {})
    if not isinstance(settings, dict):
        return ""

    vnext = settings.get("vnext")
    if isinstance(vnext, list) and vnext:
        first = vnext[0]
        if isinstance(first, dict):
            address = first.get("address")
            if isinstance(address, str):
                return address

    servers = settings.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            address = first.get("address")
            if isinstance(address, str):
                return address

    address = settings.get("address")
    if isinstance(address, str):
        return address

    return ""


def _load_group_rules() -> list[dict]:
    if not GROUP_RULES_PATH:
        return []

    path = Path(GROUP_RULES_PATH)
    if not path.exists() or not path.is_file():
        return []

    stat = path.stat()
    mtime = stat.st_mtime
    if (
        _group_rules_cache["path"] == str(path)
        and _group_rules_cache["mtime"] == mtime
    ):
        return _group_rules_cache["rules"]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _group_rules_cache.update({"path": str(path), "mtime": mtime, "rules": []})
        return []

    if isinstance(raw, dict):
        raw_rules = raw.get("groups", [])
    elif isinstance(raw, list):
        raw_rules = raw
    else:
        raw_rules = []

    normalized: list[dict] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        remarks = item.get("remarks", [])
        remark_regex = item.get("remark_regex", [])
        address_regex = item.get("address_regex", [])

        if not isinstance(remarks, list):
            remarks = []
        if not isinstance(remark_regex, list):
            remark_regex = []
        if not isinstance(address_regex, list):
            address_regex = []

        normalized.append(
            {
                "name": name.strip(),
                "remarks": [x for x in remarks if isinstance(x, str) and x.strip()],
                "remark_regex": [
                    x for x in remark_regex if isinstance(x, str) and x.strip()
                ],
                "address_regex": [
                    x for x in address_regex if isinstance(x, str) and x.strip()
                ],
                "strategy": (
                    item.get("strategy", DEFAULT_BALANCER_STRATEGY)
                    if isinstance(item.get("strategy"), str)
                    else DEFAULT_BALANCER_STRATEGY
                ),
                "probe_url": item.get("probe_url")
                if isinstance(item.get("probe_url"), str)
                else PROBE_URL,
                "probe_interval": item.get("probe_interval")
                if isinstance(item.get("probe_interval"), str)
                else PROBE_INTERVAL,
            }
        )

    _group_rules_cache.update({"path": str(path), "mtime": mtime, "rules": normalized})
    return normalized


def _rule_matches(rule: dict, remark: str, address: str) -> bool:
    if remark in rule["remarks"]:
        return True

    for pattern in rule["remark_regex"]:
        try:
            if re.search(pattern, remark, flags=re.IGNORECASE):
                return True
        except re.error:
            continue

    for pattern in rule["address_regex"]:
        try:
            if re.search(pattern, address, flags=re.IGNORECASE):
                return True
        except re.error:
            continue

    return False


def _transform_configs_with_rules(configs: list[dict], rules: list[dict]) -> list[dict]:
    if not rules:
        return []

    indexed = []
    for idx, config in enumerate(configs):
        outbound = _extract_proxy_outbound(config)
        indexed.append(
            {
                "idx": idx,
                "config": config,
                "remark": config.get("remarks", "") if isinstance(config, dict) else "",
                "address": _extract_outbound_address(outbound),
            }
        )

    consumed: set[int] = set()
    output: list[tuple[int, dict]] = []

    for rule in rules:
        matched = [
            item
            for item in indexed
            if item["idx"] not in consumed
            and _rule_matches(rule, item["remark"], item["address"])
        ]

        if len(matched) < 2:
            continue

        balancer_config = build_balancer_config(
            [item["config"] for item in matched],
            balancer_name=rule["name"],
            strategy=rule["strategy"] or DEFAULT_BALANCER_STRATEGY,
            probe_url=rule["probe_url"] or PROBE_URL,
            probe_interval=rule["probe_interval"] or PROBE_INTERVAL,
        )

        if not balancer_config:
            continue

        for item in matched:
            consumed.add(item["idx"])

        first_idx = min(item["idx"] for item in matched)
        output.append((first_idx, balancer_config))

    if not output:
        return []

    for item in indexed:
        if item["idx"] not in consumed:
            output.append((item["idx"], item["config"]))

    output.sort(key=lambda x: x[0])
    return [item[1] for item in output]


def build_balancer_config(
    configs: list[dict],
    balancer_name: str,
    strategy: str = DEFAULT_BALANCER_STRATEGY,
    probe_url: str = PROBE_URL,
    probe_interval: str = PROBE_INTERVAL,
) -> dict | None:
    """Build single xray config with balancer from multiple configs"""

    if not configs or len(configs) < 2:
        return None

    # Use first config as base
    base = copy.deepcopy(configs[0])

    outbounds = []
    balancer_selectors = []

    # Extract proxy outbounds from each config
    for i, config in enumerate(configs):
        tag = f"proxy_{i+1}"

        outbound = _extract_proxy_outbound(config)
        if outbound:
            new_outbound = copy.deepcopy(outbound)
            new_outbound["tag"] = tag
            outbounds.append(new_outbound)
            balancer_selectors.append(tag)

    if len(outbounds) < 2:
        return None

    # Add direct and block outbounds
    outbounds.extend([
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"}
    ])

    # Build final config
    final_config = {
        "remarks": balancer_name,
        "dns": base.get("dns", {
            "servers": ["1.1.1.1", "1.0.0.1"],
            "queryStrategy": "UseIP"
        }),
        "inbounds": base.get("inbounds", [
            {
                "tag": "socks",
                "port": 10808,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": False,
                    "destOverride": ["http", "tls", "quic"]
                }
            },
            {
                "tag": "http",
                "port": 10809,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {"allowTransparent": False},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": False,
                    "destOverride": ["http", "tls", "quic"]
                }
            }
        ]),
        "outbounds": outbounds,
        "routing": {
            "domainMatcher": "hybrid",
            "domainStrategy": "IPIfNonMatch",
            "balancers": [
                {
                    "tag": "balancer",
                    "selector": balancer_selectors,
                    "strategy": {
                        "type": strategy
                    }
                }
            ],
            "rules": [
                {
                    "type": "field",
                    "protocol": ["bittorrent"],
                    "outboundTag": "direct"
                },
                {
                    "type": "field",
                    "network": "tcp,udp",
                    "balancerTag": "balancer"
                }
            ]
        },
        "observatory": {
            "subjectSelector": balancer_selectors,
            "probeURL": probe_url,
            "probeInterval": probe_interval,
            "enableConcurrency": True
        }
    }

    return final_config


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/{short_uuid}")
async def proxy_subscription(short_uuid: str, request: Request):
    """Proxy subscription request and transform to balancer config"""

    user_agent = request.headers.get("User-Agent", "")

    # Only transform for Happ clients
    is_happ = "Happ" in user_agent

    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await client.get(
                f"{UPSTREAM_URL}/{short_uuid}",
                headers={
                    "User-Agent": user_agent,
                    "Accept": request.headers.get("Accept", "*/*"),
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": FORWARDED_HOST,
                    "X-Forwarded-For": request.client.host if request.client else "127.0.0.1",
                },
                timeout=30.0
            )
        except Exception as e:
            return Response(status_code=502, content=f"Upstream error: {e}")
    
    if upstream_response.status_code != 200:
        return Response(
            status_code=upstream_response.status_code,
            content=upstream_response.content
        )

    # If not Happ - return as is
    if not is_happ:
        return Response(
            content=upstream_response.content,
            media_type=upstream_response.headers.get("content-type", "text/plain")
        )

    response_headers = _extract_subscription_headers(upstream_response)

    # Try to parse and transform JSON
    try:
        configs = json.loads(upstream_response.text)

        if isinstance(configs, list):
            # Rules mode: if GROUP_RULES_PATH is configured, use selective grouping only.
            if GROUP_RULES_PATH:
                group_rules = _load_group_rules()
                transformed = _transform_configs_with_rules(configs, group_rules)
                if transformed:
                    return JSONResponse(content=transformed, headers=response_headers)
                return JSONResponse(content=configs, headers=response_headers)

            # Backward-compatible mode: if no rules configured, combine all.
            if len(configs) > 1:
                balancer_config = build_balancer_config(
                    configs,
                    balancer_name=BALANCER_NAME,
                    strategy=DEFAULT_BALANCER_STRATEGY,
                    probe_url=PROBE_URL,
                    probe_interval=PROBE_INTERVAL,
                )
                if balancer_config:
                    return JSONResponse(content=[balancer_config], headers=response_headers)

            return JSONResponse(content=configs, headers=response_headers)

        # Non-list JSON - return as is
        return Response(
            content=upstream_response.content,
            media_type="application/json",
            headers=response_headers,
        )

    except json.JSONDecodeError:
        # Not JSON - return as is
        return Response(
            content=upstream_response.content,
            media_type=upstream_response.headers.get("content-type", "text/plain"),
            headers=response_headers,
        )


@app.get("/{short_uuid}/{path:path}")
async def proxy_subscription_path(short_uuid: str, path: str, request: Request):
    """Proxy other subscription paths without transformation"""
    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await client.get(
                f"{UPSTREAM_URL}/{short_uuid}/{path}",
                headers={
                    "User-Agent": request.headers.get("User-Agent", ""),
                    "Accept": request.headers.get("Accept", "*/*"),
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": FORWARDED_HOST,
                    "X-Forwarded-For": request.client.host if request.client else "127.0.0.1",
                },
                timeout=30.0
            )
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                media_type=upstream_response.headers.get("content-type")
            )
        except Exception as e:
            return Response(status_code=502, content=f"Upstream error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("APP_PORT", "3020"))
    uvicorn.run(app, host="0.0.0.0", port=port)
