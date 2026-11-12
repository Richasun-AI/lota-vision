"""Scenario configuration and durable job storage."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Spec):
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1)
    recursive: bool = True
    site_pattern: str | None = None
    checkpoint: str | None = None
    manifest_path: str | None = None
    exclude: list[str] = Field(default_factory=list, max_length=50)


class LabelStudio(Spec):
    base_url: str
    project_id: int = Field(gt=0)
    token_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z_0-9]*$")
    media_base: str | None = None
    local_files_root: str | None = None

    @model_validator(mode="after")
    def check_connection(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Label Studio requires an HTTP(S) service URL")
        if not self.media_base and not self.local_files_root:
            raise ValueError("Label Studio requires a media URL or local files root")
        return self


class AnnotationTask(Spec):
    name: str = Field(min_length=1,max_length=100)
    task: Literal["detect","classify","segment","obb","pose"]
    classes: list[str] = Field(min_length=1,max_length=500)
    require_site: bool = False
    label_studio: LabelStudio | None = None

    @field_validator("classes")
    @classmethod
    def unique_labels(cls,values):
        import re
        if len(values)!=len(set(values)) or any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}",v) for v in values):
            raise ValueError("Task classes must be distinct portable codes")
        return values


class Category(Spec):
    name: str = Field(min_length=1, max_length=100)
    name_en: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True


class Scenario(Spec):
    name: str = Field(min_length=1, max_length=100)
    task: Literal["detect", "classify", "segment", "obb", "pose"] = "detect"
    classes: list[str] = Field(min_length=1, max_length=500)
    require_site: bool = False
    sources: list[Source] = Field(default_factory=list, max_length=100)
    scan_interval_seconds: int = Field(default=0, ge=0, le=86400)
    scan_limit: int = Field(default=5000, ge=1, le=1000000)
    label_studio: LabelStudio | None = None
    routes: dict[str, str] = Field(default_factory=dict)
    categories: dict[str, Category] = Field(default_factory=dict)
    annotation_tasks: dict[str, AnnotationTask] = Field(default_factory=dict)

    @field_validator("classes")
    @classmethod
    def check_classes(cls, values):
        import re
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", x) or x in {"con", "nul", "prn", "aux"}
            for x in values
        ):
            raise ValueError("Classes must be unique lowercase codes safe for folder names")
        return values

    @model_validator(mode="after")
    def check_scan(self):
        if 0 < self.scan_interval_seconds < 30:
            raise ValueError("Automatic scan interval must be at least 30 seconds")
        if len({source.name for source in self.sources}) != len(self.sources):
            raise ValueError("Source names must be unique")
        import re
        if any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) for key in self.categories):
            raise ValueError("Category codes must be stable lowercase identifiers")
        for task_id in self.annotation_tasks:
            validate_id(task_id)
        if not set(self.routes).issubset(self.classes):
            raise ValueError("Route keys must be configured classes")
        return self


class Step(Spec):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    tool: str
    position: dict[str, float] = Field(default_factory=dict)
    args: dict = Field(default_factory=dict)


class Workflow(Spec):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1, max_length=30)

    @field_validator("steps")
    @classmethod
    def distinct_steps(cls, values):
        if len({step.id for step in values}) != len(values):
            raise ValueError("Workflow step IDs must be unique")
        return values


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def unpack(row, *fields):
    result = dict(row)
    for field in fields:
        result[field] = json.loads(result[field]) if result[field] is not None else None
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, config TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0, next_scan REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_revisions (
    project_id TEXT NOT NULL, revision INTEGER NOT NULL, config TEXT NOT NULL,
    created REAL NOT NULL, PRIMARY KEY(project_id, revision)
);
CREATE TABLE IF NOT EXISTS workflows (
    project_id TEXT NOT NULL, id TEXT NOT NULL, definition TEXT NOT NULL,
    PRIMARY KEY(project_id, id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL,
    kind TEXT NOT NULL, spec TEXT NOT NULL, inputs TEXT NOT NULL, outputs TEXT NOT NULL,
    status TEXT NOT NULL, current_step TEXT, error TEXT, worker TEXT, lease REAL,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created DESC);
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "engine.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def project_dir(self, project_id):
        validate_id(project_id)
        directory = self.root / "projects" / project_id
        if not directory.resolve().is_relative_to(self.root / "projects"):
            raise ValueError("Project directory is outside the managed data root")
        return directory

    def save_project(self, project_id, config, expected_revision=None):
        directory = self.project_dir(project_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            current = row["revision"] if row else 0
            if current and expected_revision != current:
                raise ValueError(f"Project revision changed or missing. Current revision is {current}")
            if not current and expected_revision not in (0, None):
                raise ValueError("Project does not exist")
            revision = current + 1
            encoded = dumps(config.model_dump(mode="json"))
            directory.mkdir(parents=True, exist_ok=True)
            db.execute(
                """INSERT INTO projects(id,revision,config) VALUES (?,?,?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, config=excluded.config, next_scan=0""",
                (project_id, revision, encoded),
            )
            db.execute("INSERT INTO project_revisions VALUES (?,?,?,?)",
                       (project_id, revision, encoded, time.time()))
        return self.project(project_id)

    def project(self, project_id):
        validate_id(project_id)
        with self.connection() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise FileNotFoundError(f"Unknown scenario: {project_id}")
        return unpack(row, "config")

    def scenario(self, project_id, revision):
        with self.connection() as db:
            row = db.execute("SELECT config FROM project_revisions WHERE project_id=? AND revision=?",
                             (project_id, revision)).fetchone()
        if not row:
            raise FileNotFoundError(f"Unknown scenario revision: {project_id}/{revision}")
        return Scenario.model_validate_json(row["config"])

    def projects(self):
        with self.connection() as db:
            return [unpack(row, "config") for row in db.execute("SELECT * FROM projects ORDER BY id")]

    def activate(self, project_id, active):
        self.project(project_id)
        with self.connection() as db:
            db.execute("UPDATE projects SET active=?, next_scan=0 WHERE id=?", (active, project_id))
        return self.project(project_id)

    def save_workflow(self, project_id, workflow_id, spec):
        self.project(project_id)
        validate_id(workflow_id)
        with self.connection() as db:
            db.execute("""INSERT INTO workflows VALUES (?,?,?)
                ON CONFLICT(project_id,id) DO UPDATE SET definition=excluded.definition""",
                (project_id, workflow_id, dumps(spec.model_dump(mode="json"))))
        return {"id": workflow_id, "definition": spec.model_dump(mode="json")}

    def workflows(self, project_id):
        self.project(project_id)
        with self.connection() as db:
            return [unpack(row, "definition") for row in db.execute(
                "SELECT id,definition FROM workflows WHERE project_id=? ORDER BY id", (project_id,))]

    def workflow(self, project_id, workflow_id):
        for row in self.workflows(project_id):
            if row["id"] == workflow_id:
                return Workflow.model_validate(row["definition"])
        raise FileNotFoundError(f"Unknown workflow: {workflow_id}")

    def _enqueue(self, db, project_id, revision, kind, spec, inputs, now):
        job_id = uuid.uuid4().hex
        db.execute("""INSERT INTO jobs(id,project_id,revision,kind,spec,inputs,outputs,status,created,updated)
            VALUES (?,?,?,?,?,?,?,'queued',?,?)""",
            (job_id, project_id, revision, kind, dumps(spec.model_dump(mode="json")), dumps(inputs), "{}", now, now))
        return job_id

    def enqueue(self, project_id, kind, spec, inputs=None):
        project = self.project(project_id)
        if not project["active"]:
            raise ValueError("Start the scenario before submitting jobs")
        with self.connection() as db:
            job_id = self._enqueue(db, project_id, project["revision"], kind, spec, inputs or {}, time.time())
        return self.job(project_id, job_id)

    def schedule_scans(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            projects = list(db.execute("SELECT * FROM projects WHERE active=1 AND next_scan<=?", (now,)))
            for project in projects:
                config = Scenario.model_validate_json(project["config"])
                interval = config.scan_interval_seconds
                if not interval or not config.sources:
                    continue
                pending = db.execute("""SELECT 1 FROM jobs WHERE project_id=? AND
                    kind='catalog.scan' AND status IN ('queued','running') LIMIT 1""", (project["id"],)).fetchone()
                if not pending:
                    spec = Workflow(name="Scheduled scan", steps=[Step(id="scan", tool="catalog.scan")])
                    self._enqueue(db, project["id"], project["revision"], "catalog.scan", spec, {}, now)
                db.execute("UPDATE projects SET next_scan=? WHERE id=?", (now + interval, project["id"]))

    def job(self, project_id, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
        if not row:
            raise FileNotFoundError(f"Unknown job: {job_id}")
        return unpack(row, "spec", "inputs", "outputs")

    def jobs(self, project_id, limit=50):
        self.project(project_id)
        with self.connection() as db:
            return [dict(row) for row in db.execute("""SELECT id,project_id,revision,kind,status,current_step,error,created,updated
                FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT ?""", (project_id, limit))]

    def claim(self, worker, ttl):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE jobs SET status='interrupted', error='Worker lease expired',worker=NULL,
                lease=NULL,updated=? WHERE status='running' AND lease<?""", (now, now))
            row = db.execute("""SELECT jobs.* FROM jobs JOIN projects ON projects.id=jobs.project_id
                WHERE jobs.status='queued' AND projects.active=1 ORDER BY jobs.created LIMIT 1""").fetchone()
            if not row:
                return None
            db.execute("""UPDATE jobs SET status='running',worker=?,lease=?,updated=?,error=NULL
                WHERE id=?""", (worker, now + ttl, now, row["id"]))
            return unpack(row, "spec", "inputs", "outputs")

    def heartbeat(self, job_id, worker, ttl):
        with self.connection() as db:
            return bool(db.execute("""UPDATE jobs SET lease=? WHERE id=? AND worker=? AND status='running'""",
                                   (time.time() + ttl, job_id, worker)).rowcount)

    def checkpoint(self, job_id, worker, outputs, current_step, status="running", error=None):
        with self.connection() as db:
            updated = db.execute("""UPDATE jobs SET outputs=?, current_step=?, status=?, error=?, updated=?,
                worker=CASE WHEN ?='running' THEN worker END, lease=CASE WHEN ?='running' THEN lease END
                WHERE id=? AND worker=? AND status='running'""",
                (dumps(outputs), current_step, status, error, time.time(), status, status, job_id, worker)).rowcount
            if not updated:
                raise RuntimeError("Worker no longer owns this job")

    def change_job(self, project_id, job_id, command):
        transitions = {
            "pause": ("paused", ("queued",)),
            "resume": ("queued", ("paused", "failed", "interrupted")),
            "cancel": ("cancelled", ("queued", "paused", "failed", "interrupted")),
        }
        new, eligible = transitions[command]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
            if not row:
                raise FileNotFoundError(f"Unknown job: {job_id}")
            if row["status"] not in eligible:
                raise ValueError(f"Cannot {command} a {row['status']} job")
            db.execute("UPDATE jobs SET status=?, updated=?, error=NULL WHERE id=?",
                       (new, time.time(), job_id))
        return self.job(project_id, job_id)


def validate_id(value):
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value) or value in {"con", "nul", "prn", "aux"}:
        raise ValueError(f"Invalid scenario or workflow identifier: {value}")
    return value
