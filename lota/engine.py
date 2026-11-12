"""Scenario-scoped orchestration and checkpointed tool execution."""

import logging
import re
import threading
import uuid
from pathlib import Path

from pydantic import ValidationError

from .store import Store, Step, Workflow
from .tools import Context, TOOLS
from . import flow  # noqa: F401  注册新工具（导入文件夹、导入 YOLO 标签、覆盖率判定），worker 也要能执行

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, home, source_roots=(), weights_roots=()):
        self.store = Store(home)
        inbox = self.store.root / "imports"
        weights = self.store.root / "weights"
        inbox.mkdir(exist_ok=True)
        weights.mkdir(exist_ok=True)
        self.source_roots = (inbox, *[Path(path).expanduser().resolve() for path in source_roots])
        self.weights_roots = (weights, *[Path(path).expanduser().resolve() for path in weights_roots])

    def validate_sources(self, directory, scenario):
        for item in scenario.sources:
            source = Path(item.path).expanduser()
            if not source.is_absolute():
                raise ValueError("Source paths must be absolute")
            source = self.allowed_source(source, directory=True)
            managed = self.store.root / "projects"
            if source.is_relative_to(managed) or managed.is_relative_to(source):
                raise ValueError("Source and managed scenario storage overlap")
            if item.site_pattern:
                try:
                    pattern = re.compile(item.site_pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid site pattern: {exc}") from exc
                if "site" not in pattern.groupindex:
                    raise ValueError("site_pattern must use a named 'site' group")
            if item.manifest_path:
                self.allowed_source(Path(item.manifest_path), directory=False)

    @staticmethod
    def scoped_path(raw, roots, directory=False):
        requested = Path(raw).expanduser()
        if not requested.is_absolute():
            raise ValueError("Path must be absolute")
        path = requested.resolve()
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("Path is outside the configured allowed directories")
        if not (path.is_dir() if directory else path.is_file()):
            raise FileNotFoundError(f"Path is unavailable: {path}")
        return path

    def allowed_source(self, raw, directory=False):
        return self.scoped_path(raw, self.source_roots, directory)

    def allowed_weights(self, raw, directory=False):
        return self.scoped_path(raw, self.weights_roots, directory)

    def image_path(self, directory, record, raw_only):
        if record.get("raw_path"):
            path = Path(record["raw_path"])
            if not path.is_absolute():
                path = directory / path
            return self.scoped_path(path, (directory / "raw",))
        if raw_only:
            raise ValueError(f"Extract the selected image before using it: {record['image_id']}")
        return self.allowed_source(record.get("source_path") or record["path"])

    def save(self, project_id, config, revision=None):
        self.validate_sources(self.store.project_dir(project_id), config)
        return self.store.save_project(project_id, config, revision)

    def save_workflow(self, project_id, workflow_id, spec):
        verify_workflow(spec)
        return self.store.save_workflow(project_id, workflow_id, spec)

    def enqueue_action(self, project_id, tool_id, arguments):
        tool = TOOLS.get(tool_id)
        if not tool:
            raise FileNotFoundError(f"Unknown tool: {tool_id}")
        tool.model.model_validate({key:value for key,value in arguments.items() if key!="annotation_task"})
        spec = Workflow(name=tool.title, steps=[Step(id="result", tool=tool_id, args=arguments)])
        return self.store.enqueue(project_id, tool_id, spec)

    def enqueue_workflow(self, project_id, workflow_id, inputs):
        spec = self.store.workflow(project_id, workflow_id)
        verify_workflow(spec, inputs)
        return self.store.enqueue(project_id, "workflow:" + workflow_id, spec, inputs)

    def run_once(self, worker=None, lease=90):
        worker = worker or uuid.uuid4().hex
        job = self.store.claim(worker, lease)
        if job is None:
            return None
        end = threading.Event()

        def keep_lease():
            while not end.wait(lease / 3):
                try:
                    if not self.store.heartbeat(job["id"], worker, lease):
                        return
                except Exception:
                    logger.exception("Unable to renew worker lease for %s", job["id"])

        thread = threading.Thread(target=keep_lease, daemon=True)
        thread.start()
        outputs = job["outputs"]
        current = None
        try:
            config = self.store.scenario(job["project_id"], job["revision"])
            directory = self.store.project_dir(job["project_id"])
            workflow = Workflow.model_validate(job["spec"])
            for step in workflow.steps:
                if step.id in outputs:
                    continue
                current = step.id
                self.store.checkpoint(job["id"], worker, outputs, current)
                args = resolve_values(step.args, outputs, job["inputs"])
                ref = step.args.get("image_ids") if isinstance(step.args, dict) else None
                if isinstance(ref, dict) and "$ref" in ref and args.get("image_ids") == []:
                    # 上一步没有找到 / 产出图片：这一步没东西可做，跳过（不算失败），后面的步骤照常
                    outputs[current] = {"image_ids": [], "skipped": "上一步没有图片，这一步跳过"}
                    self.store.checkpoint(job["id"], worker, outputs, current)
                    continue
                context = Context(directory, config, self, job["id"], current)
                outputs[current] = TOOLS[step.tool].call(context, args)
                self.store.checkpoint(job["id"], worker, outputs, current)
            self.store.checkpoint(job["id"], worker, outputs, None, "succeeded")
        except Exception as exc:
            if not isinstance(exc, (ValueError, FileNotFoundError, RuntimeError, ValidationError)):
                logger.exception("Job %s failed", job["id"])
            self.store.checkpoint(job["id"], worker, outputs, current, "failed", str(exc)[:2000])
        finally:
            end.set()
            thread.join()
        return self.store.job(job["project_id"], job["id"])


REFERENCE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")


def verify_workflow(spec, inputs=None):
    known = set()
    for step in spec.steps:
        if step.tool not in TOOLS:
            raise ValueError(f"Unknown workflow tool: {step.tool}")
        verify_refs(step.args, known, inputs)
        known.add(step.id)


def verify_refs(value, known, inputs):
    if isinstance(value, dict):
        if set(value) in ({"$ref"}, {"$input"}):
            key, address = next(iter(value.items()))
            if not isinstance(address, str) or not REFERENCE.fullmatch(address):
                raise ValueError("Invalid workflow reference")
            root = address.split(".")[0]
            if key == "$ref" and root not in known:
                raise ValueError(f"Workflow reference must point to a previous step: {address}")
            if key == "$input" and inputs is not None:
                lookup(inputs, address)
            return
        for part in value.values():
            verify_refs(part, known, inputs)
    elif isinstance(value, list):
        for part in value:
            verify_refs(part, known, inputs)


def lookup(root, address):
    try:
        for key in address.split("."):
            root = root[int(key)] if isinstance(root, list) else root[key]
        return root
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"工作流里找不到 {address}：可能连线连到了已经删除的节点，或上一步没有这个输出。"
                         f"到工作流页面重新连线、保存后再运行 / Workflow reference not found: {address}") from exc


def resolve_values(value, outputs, inputs):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return lookup(outputs, value["$ref"])
        if set(value) == {"$input"}:
            return lookup(inputs, value["$input"])
        return {key: resolve_values(part, outputs, inputs) for key, part in value.items()}
    if isinstance(value, list):
        return [resolve_values(part, outputs, inputs) for part in value]
    return value
