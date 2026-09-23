"""Running-container list via the Docker Engine API over its local Unix
socket (bind-mounted read-only into the container -- see
docker-compose.yml). No docker CLI/SDK needed: httpx can talk to a Unix
socket directly, and the JSON it returns is already exactly what's needed.
"""
import httpx

DOCKER_SOCKET = "/var/run/docker.sock"


async def fetch_containers() -> dict | None:
    try:
        transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            resp = await client.get("http://docker/containers/json")
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, OSError):
        return None

    containers = [
        {
            "name": (c.get("Names") or ["?"])[0].lstrip("/"),
            "image": c.get("Image"),
            "status": c.get("Status"),
        }
        for c in data
    ]
    containers.sort(key=lambda c: c["name"])
    return {"containers": containers, "count": len(containers)}
