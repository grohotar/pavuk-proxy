"""
Subscription Proxy with Balancer
Transforms multiple Xray configs into single config with load balancing
For use with Remnawave panel and Happ client
"""

import os
import json
import copy
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="Subscription Proxy", version="1.0.0")

# Configuration from environment
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://127.0.0.1:3011")
BALANCER_NAME = os.getenv("BALANCER_NAME", "🇵🇱 Польша")
PROBE_URL = os.getenv("PROBE_URL", "https://www.google.com/generate_204")
PROBE_INTERVAL = os.getenv("PROBE_INTERVAL", "10s")


def build_balancer_config(configs: list) -> dict:
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
        
        for outbound in config.get("outbounds", []):
            if outbound.get("protocol") in ["vless", "vmess", "trojan", "shadowsocks"]:
                new_outbound = copy.deepcopy(outbound)
                new_outbound["tag"] = tag
                outbounds.append(new_outbound)
                balancer_selectors.append(tag)
                break
    
    if len(outbounds) < 2:
        return None
    
    # Add direct and block outbounds
    outbounds.extend([
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"}
    ])
    
    # Build final config
    final_config = {
        "remarks": BALANCER_NAME,
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
                        "type": "random"
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
            "probeURL": PROBE_URL,
            "probeInterval": PROBE_INTERVAL,
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
                    "X-Forwarded-Host": "subs.pavuka.cv",
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
    
    # Try to parse and transform JSON
    try:
        configs = json.loads(upstream_response.text)
        
        if isinstance(configs, list) and len(configs) > 1:
            # Multiple configs - combine into balancer
            balancer_config = build_balancer_config(configs)
            if balancer_config:
                # Copy important headers from upstream
                headers = {}
                for header_name in ['profile-title', 'profile-update-interval', 'subscription-userinfo', 'profile-web-page-url', 'support-url']:
                    if header_name in upstream_response.headers:
                        headers[header_name] = upstream_response.headers[header_name]
                
                # Return as array with single config (Happ expects array)
                return JSONResponse(content=[balancer_config], headers=headers)
        
        # Single config or failed - return as is
        return Response(
            content=upstream_response.content,
            media_type="application/json"
        )
        
    except json.JSONDecodeError:
        # Not JSON - return as is
        return Response(
            content=upstream_response.content,
            media_type=upstream_response.headers.get("content-type", "text/plain")
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
                    "X-Forwarded-Host": "subs.pavuka.cv",
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
