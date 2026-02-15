"""
Subscription Proxy with Balancer
Transforms multiple Xray configs into single config with load balancing
For use with Remnawave panel and Happ client
"""

import os
import json
import copy
import re
import asyncio
import time
import hashlib
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
DEFAULT_GROUP_MODE = os.getenv("DEFAULT_GROUP_MODE", "sticky").strip().lower() or "sticky"
GROUP_RULES_PATH = os.getenv("GROUP_RULES_PATH", "").strip()
UPSTREAM_RETRIES = max(1, int(os.getenv("UPSTREAM_RETRIES", "3")))
UPSTREAM_RETRY_DELAY_MS = max(0, int(os.getenv("UPSTREAM_RETRY_DELAY_MS", "150")))
ASSET_COOKIE_SAFETY_MARGIN_SECONDS = max(
    0, int(os.getenv("ASSET_COOKIE_SAFETY_MARGIN_SECONDS", "15"))
)

PROXY_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}
IMPORTANT_HEADERS = [
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "profile-web-page-url",
    "support-url",
]
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
PROXY_GENERATED_RESPONSE_HEADERS = {
    "date",
    "server",
}
DECODE_SENSITIVE_HEADERS = {
    # httpx может распаковывать body (gzip/deflate/br), но заголовки остаются.
    # Если отдать распакованный body + исходный Content-Encoding, браузер
    # падает с ERR_CONTENT_DECODING_FAILED.
    "content-encoding",
    "content-length",
}

_group_rules_cache: dict[str, Any] = {
    "path": None,
    "mtime": None,
    "rules": [],
}
_asset_cookie_cache: dict[str, Any] = {
    "cookie": None,
    "expires_at": 0.0,
}


def _cache_asset_cookie_from_set_cookie(set_cookie_value: str) -> None:
    if not set_cookie_value:
        return

    cookie = set_cookie_value.split(";", 1)[0].strip()
    if not cookie or "=" not in cookie:
        return

    max_age_seconds: int | None = None
    m = re.search(r"(?i)\\bmax-age=(\\d+)\\b", set_cookie_value)
    if m:
        try:
            max_age_seconds = int(m.group(1))
        except ValueError:
            max_age_seconds = None

    now = time.time()
    if max_age_seconds is not None and max_age_seconds > 0:
        expires_at = now + max(0, max_age_seconds - ASSET_COOKIE_SAFETY_MARGIN_SECONDS)
    else:
        # Fallback TTL if upstream didn't provide Max-Age (should not happen).
        expires_at = now + 25 * 60

    _asset_cookie_cache["cookie"] = cookie
    _asset_cookie_cache["expires_at"] = expires_at


def _get_cached_asset_cookie() -> str | None:
    cookie = _asset_cookie_cache.get("cookie")
    expires_at = float(_asset_cookie_cache.get("expires_at") or 0.0)
    if not cookie or time.time() >= expires_at:
        return None
    return cookie


def _extract_subscription_headers(upstream_response: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header_name in IMPORTANT_HEADERS:
        if header_name in upstream_response.headers:
            headers[header_name] = upstream_response.headers[header_name]
    return headers


def _extract_passthrough_headers(upstream_response: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in upstream_response.headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if lower_key in PROXY_GENERATED_RESPONSE_HEADERS:
            continue
        if lower_key in DECODE_SENSITIVE_HEADERS:
            continue
        headers[key] = value
    return headers


def _build_upstream_request_headers(
    request: Request, *, force_accept_html: bool = False
) -> dict[str, str]:
    accept = request.headers.get("Accept", "*/*")
    if force_accept_html and (not accept or accept.strip() == "*/*"):
        accept = "text/html"

    headers = {
        "Host": FORWARDED_HOST,
        "User-Agent": request.headers.get("User-Agent", ""),
        "Accept": accept,
        # Avoid upstream compression to prevent Content-Encoding/body mismatches.
        "Accept-Encoding": "identity",
        "Connection": "close",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": "443",
        "X-Forwarded-For": request.client.host if request.client else "127.0.0.1",
    }

    # Required for subscription-page static assets and session validation.
    cookie = request.headers.get("Cookie")
    if cookie:
        headers["Cookie"] = cookie

    referer = request.headers.get("Referer")
    if referer:
        headers["Referer"] = referer

    return headers


def _inject_cookie_if_missing(headers: dict[str, str], cookie: str | None) -> None:
    if not cookie:
        return
    # Static assets in Remnawave subscription-page can require a session cookie,
    # but module/script/link requests may be sent without cookies. We only use a
    # cached cookie as a fallback for such requests.
    if "Cookie" not in headers:
        headers["Cookie"] = cookie


def _build_upstream_url(path: str, request: Request) -> str:
    normalized_path = path.lstrip("/")
    base = UPSTREAM_URL.rstrip("/")
    target = f"{base}/{normalized_path}" if normalized_path else base
    if request.url.query:
        return f"{target}?{request.url.query}"
    return target


async def _request_upstream_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    last_error: Exception | None = None

    for attempt in range(UPSTREAM_RETRIES):
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                timeout=30.0,
            )
            if response.status_code >= 500 and attempt < (UPSTREAM_RETRIES - 1):
                await asyncio.sleep((UPSTREAM_RETRY_DELAY_MS / 1000.0) * (attempt + 1))
                continue
            return response
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.WriteTimeout,
        ) as exc:
            last_error = exc
            if attempt < (UPSTREAM_RETRIES - 1):
                await asyncio.sleep((UPSTREAM_RETRY_DELAY_MS / 1000.0) * (attempt + 1))
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError("upstream request failed")


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


def _extract_outbound_user_key(outbound: dict | None) -> str:
    """
    Extract a stable user key from proxy outbound settings.
    - vless/vmess: settings.vnext[0].users[0].id
    - trojan/ss (best-effort): password fields
    """
    if not isinstance(outbound, dict):
        return ""

    settings = outbound.get("settings", {})
    if not isinstance(settings, dict):
        return ""

    vnext = settings.get("vnext")
    if isinstance(vnext, list) and vnext:
        first = vnext[0]
        if isinstance(first, dict):
            users = first.get("users")
            if isinstance(users, list) and users:
                u0 = users[0]
                if isinstance(u0, dict):
                    user_id = u0.get("id")
                    if isinstance(user_id, str) and user_id.strip():
                        return user_id.strip()

    servers = settings.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            password = first.get("password")
            if isinstance(password, str) and password.strip():
                return password.strip()

    password = settings.get("password")
    if isinstance(password, str) and password.strip():
        return password.strip()

    return ""


def _derive_assignment_key(configs: list[dict], short_uuid: str, request: Request) -> str:
    """
    Stable key used for per-user node assignment inside a group.
    Prefer user's UUID/password from the subscription config, then fall back to short_uuid.
    """
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        outbound = _extract_proxy_outbound(cfg)
        key = _extract_outbound_user_key(outbound)
        if key:
            return key

    if short_uuid:
        return short_uuid

    if request.client and request.client.host:
        return request.client.host

    return "anonymous"


def _normalize_group_mode(value: str | None) -> str:
    v = (value or "").strip().lower()
    if not v:
        v = DEFAULT_GROUP_MODE

    if v in {"sticky", "per_user", "user", "hash", "hrw"}:
        return "sticky"
    if v in {"xray", "xray_balancer", "balancer", "client_balancer"}:
        return "xray_balancer"

    # Unknown value -> safe default.
    return "sticky"


def _hrw_pick_config(
    candidates: list[dict],
    *,
    assignment_key: str,
    group_name: str,
) -> dict:
    """
    Pick one config deterministically for this user (Rendezvous/HRW hashing).
    This gives near-even distribution and minimal churn when nodes are added/removed.
    """
    best_cfg: dict | None = None
    best_score: int = -1

    for cfg in candidates:
        outbound = _extract_proxy_outbound(cfg)
        node_key = _extract_outbound_address(outbound) or str(cfg.get("remarks") or "")
        if not node_key:
            # Last-resort, but keep deterministic-ish.
            node_key = json.dumps(cfg, sort_keys=True)[:64]

        h = hashlib.blake2s(digest_size=8)
        h.update(assignment_key.encode("utf-8", errors="ignore"))
        h.update(b"|")
        h.update(group_name.encode("utf-8", errors="ignore"))
        h.update(b"|")
        h.update(node_key.encode("utf-8", errors="ignore"))
        score = int.from_bytes(h.digest(), "big")

        if score > best_score:
            best_score = score
            best_cfg = cfg

    if best_cfg is None:
        # Should never happen (len>=1), but keep it safe.
        return candidates[0]

    return best_cfg


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
                "mode": _normalize_group_mode(
                    item.get("mode") if isinstance(item.get("mode"), str) else None
                ),
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


def _transform_configs_with_rules(
    configs: list[dict],
    rules: list[dict],
    *,
    assignment_key: str,
) -> list[dict]:
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

        mode = rule.get("mode")
        if not isinstance(mode, str):
            mode = DEFAULT_GROUP_MODE
        mode = _normalize_group_mode(mode)

        group_configs = [item["config"] for item in matched]

        if mode == "xray_balancer":
            grouped = build_balancer_config(
                group_configs,
                balancer_name=rule["name"],
                strategy=rule.get("strategy") or DEFAULT_BALANCER_STRATEGY,
                probe_url=rule.get("probe_url") or PROBE_URL,
                probe_interval=rule.get("probe_interval") or PROBE_INTERVAL,
            )
            if not grouped:
                continue
        else:
            picked = _hrw_pick_config(
                group_configs,
                assignment_key=assignment_key,
                group_name=rule["name"],
            )
            grouped = copy.deepcopy(picked)
            grouped["remarks"] = rule["name"]

        for item in matched:
            consumed.add(item["idx"])

        first_idx = min(item["idx"] for item in matched)
        output.append((first_idx, grouped))

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
        # Keep the first outbound tag as "proxy" for client compatibility
        # (some clients expect a canonical proxy tag for latency test UI).
        tag = "proxy" if i == 0 else f"proxy_{i+1}"

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


@app.api_route("/{short_uuid}", methods=["GET", "HEAD"])
async def proxy_subscription(short_uuid: str, request: Request):
    """Proxy subscription request and transform to balancer config"""

    user_agent = request.headers.get("User-Agent", "")

    # Only transform for Happ clients
    is_happ = "Happ" in user_agent

    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await _request_upstream_with_retries(
                client,
                request.method,
                _build_upstream_url(short_uuid, request),
                _build_upstream_request_headers(
                    request,
                    force_accept_html=not is_happ,
                ),
            )
        except Exception as e:
            return Response(status_code=502, content=f"Upstream error: {e}")

    # Cache session cookie from the HTML page response for later /assets requests.
    for v in upstream_response.headers.get_list("set-cookie"):
        _cache_asset_cookie_from_set_cookie(v)
        break
    
    if upstream_response.status_code != 200:
        return Response(
            status_code=upstream_response.status_code,
            content=upstream_response.content,
            media_type=upstream_response.headers.get("content-type"),
            headers=_extract_passthrough_headers(upstream_response),
        )

    # HEAD and non-Happ clients must be proxied as-is with full headers/cookies.
    if request.method != "GET" or not is_happ:
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type", "text/plain"),
            headers=_extract_passthrough_headers(upstream_response),
        )

    response_headers = _extract_subscription_headers(upstream_response)

    # Try to parse and transform JSON
    try:
        configs = json.loads(upstream_response.text)

        if isinstance(configs, list):
            # Rules mode: if GROUP_RULES_PATH is configured, use selective grouping only.
            if GROUP_RULES_PATH:
                group_rules = _load_group_rules()
                assignment_key = _derive_assignment_key(configs, short_uuid, request)
                transformed = _transform_configs_with_rules(
                    configs,
                    group_rules,
                    assignment_key=assignment_key,
                )
                if transformed:
                    return JSONResponse(content=transformed, headers=response_headers)
                return JSONResponse(content=configs, headers=response_headers)

            # Legacy mode: if no rules configured, merge all into one entry.
            if len(configs) > 1:
                mode = _normalize_group_mode(DEFAULT_GROUP_MODE)
                if mode == "xray_balancer":
                    balancer_config = build_balancer_config(
                        configs,
                        balancer_name=BALANCER_NAME,
                        strategy=DEFAULT_BALANCER_STRATEGY,
                        probe_url=PROBE_URL,
                        probe_interval=PROBE_INTERVAL,
                    )
                    if balancer_config:
                        return JSONResponse(
                            content=[balancer_config], headers=response_headers
                        )
                else:
                    assignment_key = _derive_assignment_key(configs, short_uuid, request)
                    picked = _hrw_pick_config(
                        configs,
                        assignment_key=assignment_key,
                        group_name=BALANCER_NAME,
                    )
                    grouped = copy.deepcopy(picked)
                    grouped["remarks"] = BALANCER_NAME
                    return JSONResponse(content=[grouped], headers=response_headers)

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


@app.api_route("/{short_uuid}/{path:path}", methods=["GET", "HEAD"])
async def proxy_subscription_path(short_uuid: str, path: str, request: Request):
    """Proxy other subscription paths without transformation"""
    async with httpx.AsyncClient() as client:
        try:
            headers = _build_upstream_request_headers(request)
            if short_uuid == "assets":
                _inject_cookie_if_missing(headers, _get_cached_asset_cookie())

            upstream_response = await _request_upstream_with_retries(
                client,
                request.method,
                _build_upstream_url(f"{short_uuid}/{path}", request),
                headers,
            )
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                media_type=upstream_response.headers.get("content-type"),
                headers=_extract_passthrough_headers(upstream_response),
            )
        except Exception as e:
            return Response(status_code=502, content=f"Upstream error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("APP_PORT", "3020"))
    uvicorn.run(app, host="0.0.0.0", port=port)
