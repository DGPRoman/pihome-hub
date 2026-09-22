"""Serving the built browser client from the hub itself.

`pihome-hub-web <https://github.com/DGPRoman/pihome-hub-web>`_ compiles to a static
bundle — one HTML document, some JavaScript and CSS — with no server of its own, so
something has to hand it to the browser. Doing it from this process rather than from
an nginx in front of it keeps a Pi to one unit and one port, which is the shape the
rest of this repository already assumes.

It is off unless asked for. With no ``PIHOME_WEB_ROOT`` nothing here is reached and
the hub serves exactly the routes it served before.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The one document every client-side route resolves to. Its name is the build
#: tool's convention, not a setting: a bundle without it is not a built bundle.
INDEX = "index.html"


class WebClientError(Exception):
    """``PIHOME_WEB_ROOT`` does not point at a built web client."""


def check_web_root(root: Path) -> None:
    """Prove ``root`` holds a built bundle, before the server starts listening.

    A path typo, or a deployment that never ran the build, is an operator mistake
    and should read as one line in the journal. Left to be discovered at request
    time it is a 404 on the front page, which looks like the hub is broken rather
    than like the bundle is missing.
    """
    if not root.is_dir():
        msg = (
            f"web root {str(root)!r} is not a directory. PIHOME_WEB_ROOT should name "
            "the directory a web client build wrote, or be left unset to serve the "
            "API alone."
        )
        raise WebClientError(msg)

    if not (root / INDEX).is_file():
        msg = (
            f"web root {str(root)!r} holds no {INDEX}, so it is not a built web "
            "client. Run the client's build and point PIHOME_WEB_ROOT at what it "
            "produced."
        )
        raise WebClientError(msg)


def hub_segments(app: FastAPI) -> frozenset[str]:
    """First path segment of every route the hub answers itself.

    Read off the router rather than written down here, so a route added tomorrow is
    protected from the bundle the moment it is registered — and so this cannot drift
    the way a hardcoded ``("/v1", "/health")`` would. ``/v1/relays`` contributes
    ``v1``, ``/health`` contributes ``health``, ``/openapi.json`` itself.
    """
    paths: list[str] = []
    for route in app.routes:
        # FastAPI keeps one wrapper per include_router() call instead of flattening
        # the routes into app.routes, so there are two shapes to read. The same pair
        # is enumerated by the registered_routes fixture in tests/conftest.py.
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            paths.append(getattr(route, "path", ""))
        else:
            paths.extend(context.path for context in contexts())

    segments = {path.split("/")[1] for path in paths if path.startswith("/") and path != "/"}
    if not segments:
        # Enumeration returning nothing would not fail loudly on its own: it would
        # mount a bundle that answers /v1 with index.html. Refuse to start instead.
        msg = "route enumeration found nothing, so the web client would shadow the API"
        raise RuntimeError(msg)

    return frozenset(segments)


def _wants_a_document(scope: Scope) -> bool:
    """Whether this is a browser navigating, rather than fetching a file.

    A navigation asks for ``text/html``; a ``<script>``, a stylesheet, an image or a
    ``fetch()`` does not. That difference is what keeps a missing asset a 404:
    answering ``/assets/index-C7kKkJch.js`` with an HTML document would be served as
    JavaScript and fail as a syntax error somewhere inside it, which says nothing
    about the file being absent.

    ``Sec-Fetch-Dest: document`` would state it outright, and is the header this
    would use if it could. Browsers only send the ``Sec-Fetch-*`` family to secure
    origins, and this service speaks plain HTTP on a LAN. ``Accept`` is the signal
    that is actually present.
    """
    return "text/html" in Headers(scope=scope).get("accept", "")


class SinglePageApp(StaticFiles):
    """Static files, with anything unmatched answered by ``index.html``.

    A client-side router puts real URLs in the address bar — ``/relays``, ``/rules``
    — that exist only once the bundle is running. Refreshing one asks this server
    for a path it has no file for, and the fix is to return the document that boots
    the router and let it read the URL itself.

    Path traversal is :class:`~starlette.staticfiles.StaticFiles`'s own concern: it
    resolves each candidate and refuses anything that escapes the directory.
    """

    def __init__(self, *, directory: Path, reserved: frozenset[str]) -> None:
        super().__init__(directory=directory)
        self._reserved = reserved

    async def get_response(self, path: str, scope: Scope) -> Response:
        # What the hub answers is never the bundle's to answer instead. A mistyped
        # /v1 path has to stay a 404 from the API: replying to it with index.html
        # would hand a client an HTML page where it expects JSON, and — worse — make
        # every route look like it exists, which is exactly the map the API declines
        # to publish when docs are off.
        if path.split("/", 1)[0] in self._reserved:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND)

        try:
            return await super().get_response(path, scope)
        except HTTPException as missing:
            if missing.status_code != HTTPStatus.NOT_FOUND:
                # 405 for a method a static file cannot answer, 401 for a file this
                # process may not read. Neither describes a client-side route.
                raise
            if not _wants_a_document(scope):
                raise

        return await super().get_response(INDEX, scope)


def mount_web_client(app: FastAPI, root: Path) -> None:
    """Serve the bundle in ``root`` at ``/``, behind everything ``app`` already has.

    Mounted last on purpose. Starlette matches routes in the order they were added
    and a mount at ``/`` matches every path, so this has to be the route of last
    resort rather than the first one tried.

    Nothing here is authenticated, and that is the point: the bundle is the login
    form and the code that draws it, which a browser needs before anyone has a
    session. Everything it then asks for lives under ``/v1`` and is guarded there.
    """
    check_web_root(root)
    app.mount("/", SinglePageApp(directory=root, reserved=hub_segments(app)), name="web")
