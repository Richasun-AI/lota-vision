"""Standalone HTTP interface for the workbench and agent tools."""

import hmac
import io
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError

from . import __version__, catalog, labels, models
from .engine import Engine
from .store import Scenario, Spec, Workflow
from .tools import TOOLS
from . import flow  # 注册节点式工作流的新工具和接口


class ProjectUpdate(Spec):
    config: Scenario
    expected_revision: int | None = None


class Activate(Spec):
    active: bool


class Control(Spec):
    command: Literal["pause", "resume", "cancel"]


class WorkflowRun(Spec):
    inputs: dict


def create_app(home, source_roots=(), weights_roots=(), token=None):
    engine = Engine(home, source_roots, weights_roots)
    app = FastAPI(title="LOTA", version=__version__)
    app.state.engine = engine

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing(request: Request, exc: FileNotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    def authorize(authorization: str | None = Header(default=None)):
        if token and not hmac.compare_digest((authorization or "").encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "Valid operator token required", headers={"WWW-Authenticate": "Bearer"})

    router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    def directory(project_id):
        engine.store.project(project_id)
        return engine.store.project_dir(project_id)

    @app.get("/api/health", tags=["engine"])
    def health():
        return {"status": "ok", "engine": "LOTA", "version": __version__}

    @router.get("/tools", tags=["agent"])
    def tools():
        return [tool.describe(name) for name, tool in TOOLS.items()]

    @router.get("/projects", tags=["scenarios"])
    def projects():
        return engine.store.projects()

    @router.get("/projects/{project_id}", tags=["scenarios"])
    def project(project_id: str):
        return engine.store.project(project_id)

    @router.put("/projects/{project_id}", tags=["scenarios"])
    def save_project(project_id: str, payload: ProjectUpdate):
        return engine.save(project_id, payload.config, payload.expected_revision)

    @router.post("/projects/{project_id}/activate", tags=["scenarios"])
    def activate(project_id: str, payload: Activate):
        return engine.store.activate(project_id, payload.active)

    @router.get("/projects/{project_id}/stats", tags=["catalog"])
    def stats(project_id: str):
        return catalog.stats(directory(project_id))

    @router.get("/projects/{project_id}/images", tags=["catalog"])
    def images(project_id: str, status: str | None = None, checkpoint: str | None = None,
               site_id: str | None = None, extracted: bool | None = None,
               limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return catalog.list_images(directory(project_id), status=status, checkpoint=checkpoint,
                                   site_id=site_id, extracted=extracted, limit=limit, offset=offset)

    @router.get("/projects/{project_id}/images/{image_id}/preview", tags=["catalog"])
    def preview(project_id: str, image_id: str, size: int = Query(360, ge=120, le=1800)):
        scope = directory(project_id)
        record = catalog.get_image(scope, image_id)
        key = record["image_id"]
        cache_dir = scope / "previews" / str(size) / key[:2]
        target = cache_dir / (key + ".jpg")
        cache_headers = {"Cache-Control": "private, max-age=3600"}
        if target.is_file():
            return FileResponse(target, media_type="image/jpeg", headers=cache_headers)
        path = engine.image_path(scope, record, raw_only=False)
        try:
            with Image.open(path) as source:
                if source.format == "JPEG":
                    source.draft("RGB", (size, size))
                image = ImageOps.exif_transpose(source)
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=82)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, "Image preview cannot be decoded") from exc
        data = output.getvalue()
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return Response(data, media_type="image/jpeg", headers=cache_headers)

    @router.get("/projects/{project_id}/images/{image_id}/duplicates", tags=["catalog"])
    def duplicates(project_id: str, image_id: str, limit: int = Query(20, ge=1, le=50)):
        return catalog.near_duplicates(directory(project_id), image_id, limit=limit)

    @router.get("/projects/{project_id}/sorting")
    def sorting_results(project_id: str, category: str | None = None,
                        limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        from .sorting import results
        return results(directory(project_id), category, limit, offset)

    def task_directory(project_id,annotation_task):
        from .tasks import scoped
        project=engine.store.project(project_id)
        return scoped(directory(project_id),Scenario.model_validate(project["config"]),annotation_task)[0]

    @router.get("/projects/{project_id}/annotation-tasks/{task_id}/config")
    def annotation_config(project_id: str,task_id: str):
        scenario=Scenario.model_validate(engine.store.project(project_id)["config"])
        task=scenario.annotation_tasks.get(task_id)
        if task is None:raise FileNotFoundError("Unknown annotation task")
        return {"label_config":labels.label_config(task.classes,task.task),"task":task.model_dump()}

    @router.get("/projects/{project_id}/annotations", tags=["data"])
    def annotations(project_id: str, limit: int = Query(100, ge=1, le=200),
                    offset: int = Query(0, ge=0), annotation_task: str | None = None):
        return labels.list_annotations(task_directory(project_id,annotation_task), limit=limit, offset=offset)

    @router.get("/projects/{project_id}/datasets", tags=["data"])
    def datasets(project_id: str,annotation_task: str | None = None):
        return labels.list_datasets(task_directory(project_id,annotation_task))

    @router.get("/projects/{project_id}/models", tags=["models"])
    def model_versions(project_id: str,annotation_task: str | None = None):
        return models.list_models(task_directory(project_id,annotation_task))

    @router.post("/projects/{project_id}/tools/{tool_id}", status_code=202, tags=["agent"])
    def invoke_tool(project_id: str, tool_id: str, args: dict = Body(default_factory=dict)):
        return engine.enqueue_action(project_id, tool_id, args)

    @router.get("/projects/{project_id}/workflows", tags=["workflows"])
    def workflows(project_id: str):
        return engine.store.workflows(project_id)

    @router.put("/projects/{project_id}/workflows/{workflow_id}", tags=["workflows"])
    def save_workflow(project_id: str, workflow_id: str, definition: Workflow):
        return engine.save_workflow(project_id, workflow_id, definition)

    @router.post("/projects/{project_id}/workflows/{workflow_id}/run", status_code=202, tags=["workflows"])
    def run_workflow(project_id: str, workflow_id: str, payload: WorkflowRun):
        return engine.enqueue_workflow(project_id, workflow_id, payload.inputs)

    @router.get("/projects/{project_id}/jobs", tags=["jobs"])
    def jobs(project_id: str, limit: int = Query(50, ge=1, le=200)):
        return engine.store.jobs(project_id, limit)

    @router.get("/projects/{project_id}/jobs/{job_id}", tags=["jobs"])
    def job(project_id: str, job_id: str):
        return engine.store.job(project_id, job_id)

    @router.post("/projects/{project_id}/jobs/{job_id}/control", tags=["jobs"])
    def control(project_id: str, job_id: str, payload: Control):
        return engine.store.change_job(project_id, job_id, payload.command)

    flow.register_routes(router, engine, directory)
    from . import hub
    hub.register_routes(router, engine, directory)
    hub.register_overview(router, engine, directory)
    hub.register_hooks(router, engine, directory)
    hub.register_progress(router, engine, directory)
    hub.register_plots(router, engine, directory)
    hub.register_guide(router, engine, directory)
    hub.register_io(router, engine, directory)
    hub.register_system(router, engine, directory)
    hub.register_stats(router, engine, directory)
    hub.register_try_video(router, engine, directory)
    from . import agent
    agent.register_routes(router, engine, directory)
    agent.register_reports(router, engine, directory)
    from . import media
    media.register_routes(router, engine, directory)
    from . import qa
    qa.register_routes(router, engine, directory)
    from . import phototypes
    phototypes.register_routes(router, engine, directory)
    phototypes.register_delete_routes(router, engine)
    from . import streams
    streams.register_routes(router, engine, directory, app)
    app.include_router(router)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(static / "index.html")

    @app.get("/flow", include_in_schema=False)
    def flow_page():
        return FileResponse(static / "flow.html")

    @app.get("/review", include_in_schema=False)
    def review_page():
        return FileResponse(static / "review.html")

    for _page in ("models", "datasets", "settings", "developers", "inspect", "toolbox", "overview", "guide", "report", "types", "m", "streams"):  # 产品页面（模型中心、数据集、项目设置、开发者 API）
        app.add_api_route(f"/{_page}", (lambda name: (lambda: FileResponse(static / f"{name}.html")))(_page),
                          methods=["GET"], include_in_schema=False)

    return app
