"""The Android app, for a phone that is joining and has no session yet.

Outside ``/v1`` and outside the guard, like ``/health``: the person downloading it
has nothing to authenticate with, which is the reason they are downloading it. The
app is open source, so nothing is given away. Both routes answer ``404`` until an
operator installs an APK, so a hub that offers none looks as it did before.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from pihome_hub.android import APK_MEDIA_TYPE, AndroidApp

router = APIRouter(prefix="/app", tags=["app"])

_NONE = "This hub has no Android app to offer. Install one with pihome-hub-admin app install"


class AndroidAppResponse(BaseModel):
    """What the join page needs to offer the download, and a phone to check it."""

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(description="SHA-256 of the APK, in hex")
    size: int = Field(description="Size of the APK in bytes")


def _app(request: Request) -> AndroidApp:
    app: AndroidApp = request.app.state.android_app
    return app


@router.get(
    "/android.json",
    summary="Whether this hub offers the Android app",
    responses={404: {"description": "No APK has been installed on this hub"}},
)
def describe_android_app(request: Request) -> AndroidAppResponse:
    found = _app(request).describe()
    if found is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=_NONE)
    return AndroidAppResponse(sha256=found.sha256, size=found.size)


@router.get(
    "/pihome.apk",
    summary="Download the Android app",
    response_class=FileResponse,
    responses={404: {"description": "No APK has been installed on this hub"}},
)
def download_android_app(request: Request) -> FileResponse:
    app = _app(request)
    if app.describe() is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=_NONE)
    # Revalidated on every download, so a phone never installs a release that has
    # since been replaced; the ETag keeps an unchanged one from being sent twice.
    return FileResponse(
        app.path,
        media_type=APK_MEDIA_TYPE,
        filename="pihome.apk",
        headers={"Cache-Control": "no-cache"},
    )
