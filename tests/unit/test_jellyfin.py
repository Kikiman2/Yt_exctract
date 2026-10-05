import asyncio
import json

import httpx
import pytest
import respx

from app.library.jellyfin import JellyfinNotifier

URL = "http://jf:8096"


@pytest.fixture
async def notifier():
    n = JellyfinNotifier(URL, "secret", "/data/YouTube/", "YouTube", debounce=0)
    yield n
    await n.aclose()


@pytest.fixture
def jf():
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        yield mock


def _updates(route):
    return [[(u["Path"], u["UpdateType"]) for u in json.loads(c.request.content)["Updates"]]
            for c in route.calls]


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_touch_debounces_into_one_request(jf):
    n = JellyfinNotifier(URL, "secret", "/data/YouTube/", "YouTube", debounce=0.05)
    route = jf.post("/Library/Media/Updated").respond(204)
    n.touch("YouTube/A")
    n.touch("YouTube/B")
    n.touch("YouTube/A")
    assert route.call_count == 0
    await asyncio.sleep(0.2)
    assert _updates(route) == [[("/data/YouTube/YouTube/A", "Created"),
                                ("/data/YouTube/YouTube/B", "Created")]]
    req = route.calls[0].request
    assert req.headers["Authorization"] == 'MediaBrowser Token="secret"'
    assert not n.pending
    await n.aclose()


async def test_zero_debounce_sends_soon(notifier, jf):
    route = jf.post("/Library/Media/Updated").respond(204)
    notifier.touch("A")
    await _settle()
    assert route.call_count == 1


async def test_flush_sends_now(jf):
    n = JellyfinNotifier(URL, "secret", "/lib", debounce=60)
    route = jf.post("/Library/Media/Updated").respond(204)
    n.touch("A")
    await n.flush()
    assert _updates(route) == [[("/lib/A", "Created")]]
    await n.flush()  # nothing pending: no second request
    assert route.call_count == 1
    await n.aclose()


async def test_failure_never_raises_and_keeps_pending(jf):
    n = JellyfinNotifier(URL, "secret", "/lib", debounce=60)
    route = jf.post("/Library/Media/Updated").mock(side_effect=httpx.ConnectError("down"))
    n.touch("A")
    await n.flush()
    assert n.pending == {"A"}
    route.mock(side_effect=None, return_value=httpx.Response(500))
    await n.flush()
    assert n.pending == {"A"}
    route.mock(return_value=httpx.Response(204))
    await n.flush()
    assert not n.pending
    await n.aclose()


async def test_no_api_key_is_noop(jf):
    n = JellyfinNotifier(URL, "", "/data/YouTube")
    n.touch("A")
    assert not n.pending
    await n.flush()
    assert await n.test() == {"ok": False, "detail": "JELLYFIN_API_KEY not set"}
    assert await n.create_library() == {"ok": False, "detail": "JELLYFIN_API_KEY not set"}
    assert not jf.calls
    await n.aclose()


async def test_touch_from_worker_thread(notifier, jf):
    route = jf.post("/Library/Media/Updated").respond(204)
    notifier.touch("A")  # binds the loop
    await _settle()
    await asyncio.to_thread(notifier.touch, "B")
    await _settle()
    assert route.call_count == 2
    assert _updates(route)[1] == [("/data/YouTube/B", "Created")]


async def test_test_ok(notifier, jf):
    jf.get("/System/Info").respond(200, json={"Version": "10.10.0", "ServerName": "Lenovo"})
    jf.get("/Library/VirtualFolders").respond(200, json=[
        {"Name": "YouTube", "Locations": ["/data/YouTube"]}])
    res = await notifier.test()
    assert res["ok"] is True and "Lenovo" in res["detail"] and "found" in res["detail"]
    assert jf.calls[0].request.headers["Authorization"] == 'MediaBrowser Token="secret"'


async def test_test_bad_key_and_unreachable(notifier, jf):
    jf.get("/System/Info").respond(401)
    res = await notifier.test()
    assert res["ok"] is False and "rejected" in res["detail"]
    jf.get("/System/Info").mock(side_effect=httpx.ConnectError("down"))
    res = await notifier.test()
    assert res["ok"] is False and "unreachable" in res["detail"]


async def test_create_library_is_idempotent(notifier, jf):
    existing: list[dict] = []
    jf.get("/Library/VirtualFolders").mock(side_effect=lambda req: httpx.Response(200, json=existing))

    def create(req):
        existing.append({"Name": req.url.params["name"], "Locations": req.url.params.get_list("paths")})
        return httpx.Response(204)

    post = jf.post("/Library/VirtualFolders").mock(side_effect=create)
    first = await notifier.create_library()
    assert first["ok"] is True
    params = post.calls[0].request.url.params
    assert params["name"] == "YouTube" and params["collectionType"] == "homevideos"
    assert params.get_list("paths") == ["/data/YouTube"]
    second = await notifier.create_library()
    assert second["ok"] is True and "already" in second["detail"]
    assert post.call_count == 1


async def test_create_library_name_clash_and_errors(notifier, jf):
    jf.get("/Library/VirtualFolders").respond(200, json=[{"Name": "YouTube", "Locations": ["/other"]}])
    post = jf.post("/Library/VirtualFolders").respond(204)
    res = await notifier.create_library()
    assert res["ok"] is False and post.call_count == 0
    jf.get("/Library/VirtualFolders").mock(side_effect=httpx.ConnectError("down"))
    assert (await notifier.create_library())["ok"] is False
