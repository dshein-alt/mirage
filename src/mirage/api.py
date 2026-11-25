import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from .auth import AuthContext, AuthService, SESSION_COOKIE, SESSION_SECONDS, validate_access_tokens
from .config import ConfigService
from .errors import WorkbenchError
from .security import bounded_upload
from .service import Application

application = Application(start_queue=False)
auth_service = AuthService(application.settings["server"]["mode"])


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("MIRAGE_VERIFY_ONLY") != "1":
        application.queue.start()
    yield
    application.queue.stop()


app = FastAPI(title="Mirage", version="1.0.0", lifespan=lifespan)


def error_response(code, message, status):
    response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
    return response


async def call_with_admission(request, call_next):
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"} and "/auth/" not in request.url.path:
        try:
            with application.database.write_admission():
                return await call_next(request)
        except WorkbenchError as error:
            return error_response(error.code, error.message, error.status)
    return await call_next(request)


@app.middleware("http")
async def security_and_errors(request: Request, call_next):
    auth_required = auth_service.enabled()
    is_api = request.url.path.startswith("/api/")
    is_login = request.url.path in ("/api/v1/auth/login", "/api/auth/login")
    context = AuthContext("admin", "local")
    if auth_required and is_api and not is_login:
        context = auth_service.authenticate(request)
        if context is None:
            response = error_response("unauthorized", "Bearer token or browser session required", 401)
        elif auth_service.requires_csrf(request, context) and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), context.csrf):
            response = error_response("csrf_rejected", "Missing or invalid CSRF token", 403)
        elif auth_service.requires_admin(request) and context.role != "admin":
            response = error_response("forbidden", "Administrator role required", 403)
        else:
            request.state.auth = context
            response = await call_with_admission(request, call_next)
    else:
        request.state.auth = context
        response = await call_with_admission(request, call_next)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(WorkbenchError)
async def workbench_error(request, error):
    return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return JSONResponse({"error": {"code": "request_invalid", "message": str(error)}}, status_code=422)


@app.post("/api/v1/auth/login")
@app.post("/api/auth/login")
def login(request: Request, credentials: dict):
    session_id, context = auth_service.login(credentials.get("token", ""))
    response = JSONResponse({"authenticated": True, "role": context.role, "csrf": context.csrf})
    response.set_cookie(
        SESSION_COOKIE, session_id, max_age=SESSION_SECONDS,
        httponly=True, secure=request.url.scheme == "https",
        samesite="strict", path="/",
    )
    return response


@app.get("/api/v1/auth/session")
@app.get("/api/auth/session")
def auth_session(request: Request):
    context = request.state.auth
    return {
        "authenticated": True,
        "auth_required": auth_service.enabled(),
        "role": context.role,
        "csrf": context.csrf,
    }


@app.post("/api/v1/auth/logout")
@app.post("/api/auth/logout")
def logout(request: Request):
    auth_service.logout(request)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/v1/health")
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "deployment_id": os.environ.get("MIRAGE_DEPLOYMENT_ID"),
        "container_image": os.environ.get("MIRAGE_CONTAINER_IMAGE_ID"),
        "maintenance": application.database.maintenance_status(),
    }


@app.get("/api/v1/system")
@app.get("/api/system")
def system():
    return application.system()


@app.get("/api/v1/models")
@app.get("/api/models")
def models():
    return application.registry.all_status()


@app.get("/api/v1/models/{model_id}")
@app.get("/api/models/{model_id}")
def model(model_id):
    return application.registry.status(model_id)


@app.post("/api/v1/models/{model_id}/install", status_code=202)
@app.post("/api/models/{model_id}/install", status_code=202)
def install_model(model_id, request: dict | None = None):
    request = request or {}
    return application.submit_install(model_id, bool(request.get("with_editing")))


@app.post("/api/v1/models/{model_id}/verify", status_code=202)
@app.post("/api/models/{model_id}/verify", status_code=202)
def verify_model(model_id, request: dict | None = None):
    request = request or {}
    application.registry.verify(model_id, bool(request.get("with_editing")), checksum=False)
    return application.submit_install(model_id, bool(request.get("with_editing")), verify=True)


@app.post("/api/v1/models/{model_id}/remove")
@app.post("/api/models/{model_id}/remove")
def remove_model(model_id):
    return application.remove_model(model_id)


@app.get("/api/v1/config/generation")
@app.get("/api/config/generation")
def get_config():
    return application.config.snapshot()


@app.put("/api/v1/config/generation")
@app.put("/api/config/generation")
def put_config(document: dict):
    try:
        return application.config.update(document)
    except ValueError as error:
        raise WorkbenchError("config_invalid", str(error)) from error


@app.post("/api/v1/config/generation/reload")
@app.post("/api/config/generation/reload")
def reload_config():
    return application.config.reload()


@app.post("/api/v1/config/generation/restore-defaults")
@app.post("/api/config/generation/restore-defaults")
def restore_config():
    return application.config.restore_defaults()


@app.get("/api/v1/settings")
@app.get("/api/settings")
def get_settings():
    return application.settings


@app.put("/api/v1/settings")
@app.put("/api/settings")
def put_settings(document: dict):
    try:
        return application.update_settings(document)
    except ValueError as error:
        raise WorkbenchError("settings_invalid", str(error)) from error


@app.post("/api/v1/uploads", status_code=201)
@app.post("/api/uploads", status_code=201)
async def upload(request: Request, filename: str = "upload"):
    content = await bounded_upload(request, application.settings["security"]["max_upload_bytes"])
    return application.media.upload(content, filename)


@app.delete("/api/v1/inputs/{input_id}")
@app.delete("/api/inputs/{input_id}")
def delete_input(input_id):
    application.media.delete_input(input_id)
    return {"deleted": True}


@app.post("/api/v1/jobs", status_code=202)
@app.post("/api/jobs", status_code=202)
def submit_job(request: dict):
    return application.submit_generation(request)


@app.get("/api/v1/jobs")
@app.get("/api/jobs")
def jobs(limit: int = 100, status: str | None = None):
    return application.database.list_jobs(min(max(limit, 1), 500), status)


@app.get("/api/v1/jobs/{job_id}")
@app.get("/api/jobs/{job_id}")
def job(job_id):
    return application.database.get_job(job_id)


@app.get("/api/v1/jobs/{job_id}/logs")
@app.get("/api/jobs/{job_id}/logs")
def job_logs(job_id):
    record = application.database.get_job(job_id)
    if not record["log_path"]:
        return {"log": ""}
    path = Path(record["log_path"])
    data = path.read_text(encoding="utf-8", errors="replace")[-200000:] if path.is_file() else ""
    return {"log": data}


@app.post("/api/v1/jobs/{job_id}/cancel")
@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id):
    return application.queue.cancel(job_id)


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id):
    return application.queue.retry(job_id)


@app.get("/api/v1/events")
@app.get("/api/events")
async def events(request: Request, after: int = 0):
    async def stream():
        sequence = after
        while True:
            if await request.is_disconnected():
                return
            for event in application.database.events_after(sequence):
                sequence = event["sequence"]
                yield f"id: {sequence}\nevent: {event['type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/images")
@app.get("/api/images")
def images(search: str | None = None, model: str | None = None, resolution: str | None = None, quality: str | None = None, width: int | None = None, height: int | None = None, limit: int = 200):
    return application.database.list_images(search, model, resolution, quality, width, height, min(max(limit, 1), 500))


@app.get("/api/v1/image-dimensions")
@app.get("/api/image-dimensions")
def image_dimensions():
    return application.database.image_dimensions()


@app.get("/api/v1/images/{image_id}")
@app.get("/api/images/{image_id}")
def image(image_id):
    return application.database.get_image(image_id)


@app.get("/api/v1/images/{image_id}/file")
@app.get("/api/images/{image_id}/file")
def image_file(image_id):
    return FileResponse(application.media.image_path(image_id), media_type="image/png", filename=Path(application.database.get_image(image_id)["filename"]).name)


@app.get("/api/v1/images/{image_id}/thumbnail")
@app.get("/api/images/{image_id}/thumbnail")
def image_thumbnail(image_id):
    return FileResponse(application.media.thumbnail_path(image_id), media_type="image/jpeg")


@app.delete("/api/v1/images/{image_id}")
@app.delete("/api/images/{image_id}")
def delete_image(image_id, permanent: bool = False):
    image = application.database.trash_image(image_id, permanent)
    if permanent:
        output = application.paths.outputs / image["filename"]
        output.unlink(missing_ok=True)
        output.with_suffix(".json").unlink(missing_ok=True)
        (application.paths.thumbnails / f"{Path(image['filename']).stem}.jpg").unlink(missing_ok=True)
    return {"deleted": True, "permanent": permanent}


@app.post("/api/v1/gallery/reindex")
@app.post("/api/gallery/reindex")
def reindex_gallery():
    return {"imported": application.media.reindex()}


@app.get("/api/v1/runtime")
@app.get("/api/runtime")
def runtime():
    return application.runtime.status()


@app.post("/api/v1/runtime/verify")
@app.post("/api/runtime/verify")
def verify_runtime(devices: bool = False):
    return application.runtime.verify(devices)


@app.post("/api/v1/runtime/activate/{revision}")
@app.post("/api/runtime/activate/{revision}")
def activate_runtime(revision):
    return application.runtime.activate(revision)


@app.post("/api/v1/runtime/rollback")
@app.post("/api/runtime/rollback")
def rollback_runtime():
    return application.runtime.rollback()


app.mount("/", StaticFiles(directory=application.paths.web, html=True), name="web")


def main():
    settings = application.settings["server"]
    try:
        validate_access_tokens(settings["mode"])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    default_host = "127.0.0.1" if settings["mode"] == "local" else "0.0.0.0"
    host = os.environ.get("MIRAGE_BIND_HOST", default_host)
    uvicorn.run(app, host=host, port=settings["port"], log_level="info", access_log=False)


if __name__ == "__main__":
    main()
