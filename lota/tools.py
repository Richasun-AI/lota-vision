"""Typed actions exposed to the UI, workflows and agent clients."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from .store import Spec


class ScanArgs(Spec):
    limit: int | None = Field(default=None, ge=1, le=1000000)
    stability_seconds: float = Field(default=2, ge=0, le=3600)


class Selection(Spec):
    image_ids: list[str] = Field(min_length=1, max_length=1000)


class Review(Spec):
    image_id: str
    decision: Literal["accepted", "rejected", "pending"]
    checkpoint: str | None = None
    site_id: str | None = None
    note: str | None = None


class Near(Spec):
    image_id: str
    max_distance: int = Field(default=6, ge=0, le=16)
    limit: int = Field(default=30, ge=1, le=100)


class AnnotationImport(Spec):
    tasks: list[dict] = Field(min_length=1, max_length=10000)


class Dataset(Spec):
    version: str
    val_fraction: float = Field(default=0.2, gt=0, lt=1)
    test_fraction: float = Field(default=0.0, ge=0, lt=1)
    seed: int = 42


class Register(Spec):
    version: str
    weights: str
    dataset_version: str | None = None


class Alias(Spec):
    alias: str = "production"
    version: str


class Train(Spec):
    dataset_version: str
    model_version: str
    weights: str
    epochs: int = Field(default=10, ge=1, le=2000)
    imgsz: int = Field(default=640, ge=32, le=4096)
    batch: int = Field(default=8, ge=1, le=512)
    workers: int = Field(default=0, ge=0, le=32)
    device: str = "cpu"
    seed: int = 42
    # 微调用（不填 = Ultralytics 默认）：从已训好的模型继续训时，学习率要调小，否则会冲掉已学到的
    lr0: float | None = Field(default=None, gt=0, le=1)
    optimizer: Literal["auto", "SGD", "Adam", "AdamW"] | None = None
    patience: int | None = Field(default=None, ge=0, le=1000)
    freeze: int | None = Field(default=None, ge=0, le=50)


class Evaluate(Spec):
    selector: str = "production"
    dataset_version: str
    split: Literal["val", "test"] = "val"
    imgsz: int | None = Field(default=None, ge=32, le=4096)
    device: str = "cpu"


class Predict(Selection):
    selector: str = "production"
    confidence: float = Field(default=0.25, ge=0, le=1)
    imgsz: int | None = Field(default=None, ge=32, le=4096)
    device: str = "cpu"


class Route(Spec):
    predictions: list[dict] = Field(max_length=1000)
    min_confidence: float = Field(default=0.7, ge=0, le=1)


class TextRead(Selection):
    language: str = "en"


class Curate(Selection):
    model_dir: str
    batch_size: int = Field(default=16, ge=1, le=128)
    device: Literal["cpu", "cuda"] = "cpu"


@dataclass
class Context:
    directory: Path
    scenario: object
    engine: object
    job_id: str
    step_id: str

    def image_paths(self, ids):
        from .catalog import get_image
        from .tasks import source_project
        root=source_project(self.directory)
        return [self.engine.image_path(root, get_image(root, item), raw_only=True)
                for item in dict.fromkeys(ids)]

    def label_studio(self):
        connection = self.scenario.label_studio
        if connection is None:
            raise ValueError("这个子项目还没连接 Label Studio：到“标注系统”点“提交所选图片”一键连接，或运行“连接 Label Studio”"
                             " / Configure a Label Studio connection for this scenario")
        return connection.model_dump(exclude_none=True)


@dataclass(frozen=True)
class Tool:
    title: str
    model: type[Spec]
    function: object

    def call(self, context, arguments):
        arguments=dict(arguments)
        task_id=arguments.pop("annotation_task",None)
        if task_id:
            from .tasks import task_context
            context=task_context(context,task_id)
        values = self.model.model_validate(arguments).model_dump(exclude_none=True)
        return self.function(context, **values)

    def describe(self, key):
        schema=self.model.model_json_schema()
        if key.startswith(("labels.","dataset.","model.")):
            schema["properties"]["annotation_task"]={"type":"string","title":"Annotation task"}
        return {"id": key, "title": self.title, "schema": schema}


def scan(c, limit=None, stability_seconds=2):
    from .catalog import scan
    sources = [item.model_dump(exclude_none=True) for item in c.scenario.sources]
    c.engine.validate_sources(c.directory, c.scenario)
    if not sources:
        raise ValueError("Configure a source directory before scanning")
    return scan(c.directory, sources, limit=limit or c.scenario.scan_limit, stability_seconds=stability_seconds)


def review(c, image_id, **options):
    from .catalog import review_image
    return review_image(c.directory, image_id, **options)


def extract(c, image_ids):
    from .catalog import extract, get_image
    for image_id in dict.fromkeys(image_ids):
        record = get_image(c.directory, image_id)
        if not record.get("raw_path"):
            c.engine.image_path(c.directory, record, raw_only=False)
    result = extract(c.directory, image_ids)
    result["image_ids"] = [image_id for image_id in dict.fromkeys(image_ids)
                           if get_image(c.directory, image_id).get("raw_path")]
    return result


def near(c, image_id, **options):
    from .catalog import near_duplicates
    return {"items": near_duplicates(c.directory, image_id, **options)}


def import_annotations(c, tasks):
    from .labels import import_annotations
    return import_annotations(c.directory, tasks, c.scenario.classes, task=c.scenario.task)


def push(c, image_ids):
    from .labels import push_tasks
    c.image_paths(image_ids)
    return push_tasks(c.directory, c.label_studio(), image_ids, c.scenario.classes, task=c.scenario.task)


def pull(c):
    from .labels import pull_annotations
    return pull_annotations(c.directory, c.label_studio(), c.scenario.classes, task=c.scenario.task)


def publish(c, version, **options):
    from .labels import publish_dataset
    return publish_dataset(c.directory, version, c.scenario.classes,
                           task=c.scenario.task, require_site=c.scenario.require_site, **options)


def register(c, version, weights, dataset_version=None):
    from .models import register
    return register(c.directory, version, c.engine.allowed_weights(weights),
                    classes=c.scenario.classes, task=c.scenario.task, dataset_version=dataset_version)


def alias(c, alias, version):
    from .models import set_alias
    return set_alias(c.directory, alias, version)


def train(c, dataset_version, model_version, weights, **options):
    from .models import train
    return train(c.directory, dataset_version, model_version, c.engine.allowed_weights(weights), **options)


def evaluate(c, selector, dataset_version, **options):
    from .models import evaluate
    return evaluate(c.directory, selector, dataset_version, **options)


def predict(c, selector, image_ids, **options):
    from .models import predict
    paths = c.image_paths(image_ids)
    results = predict(c.directory, selector, paths, output=f"{c.job_id}-{c.step_id}.json", **options)
    by_path = {str(path.resolve()): image_id for path, image_id in zip(paths, dict.fromkeys(image_ids))}
    for item in results.get("predictions", []):
        item["image_id"] = by_path.get(str(Path(item["path"]).resolve()))
    return results


def route(c, predictions, min_confidence):
    results = []
    for item in predictions:
        findings = item.get("classifications") or item.get("detections") or []
        if isinstance(findings, dict):
            findings = [findings]
        best = max(findings, key=lambda finding: finding["confidence"], default=None)
        label = (best.get("label") or best.get("class_name")) if best else None
        destination = c.scenario.routes.get(label)
        results.append({"image_id": item.get("image_id"), "label": label,
                        "confidence": best["confidence"] if best else None,
                        "route": destination if destination and best["confidence"] >= min_confidence else "review"})
    return {"items": results}


def read_text(c, image_ids, language):
    from .vision import read_text
    return read_text(c.image_paths(image_ids), language)


def curate(c, image_ids, model_dir, batch_size, device):
    from .vision import suggest_types
    return suggest_types(c.directory, c.image_paths(image_ids), c.scenario.classes,
                         c.engine.allowed_weights(model_dir, directory=True), batch_size, device)


TOOLS = {
    "catalog.scan": Tool("扫描原图", ScanArgs, scan),
    "catalog.review": Tool("复核照片", Review, review),
    "catalog.extract": Tool("复制选中原图", Selection, extract),
    "catalog.near": Tool("查找近重复候选", Near, near),
    "labels.import": Tool("导入标注 JSON", AnnotationImport, import_annotations),
    "labels.push": Tool("提交 Label Studio 任务", Selection, push),
    "labels.pull": Tool("回收 Label Studio 标注", Spec, pull),
    "dataset.publish": Tool("发布数据集版本", Dataset, publish),
    "model.register": Tool("注册模型", Register, register),
    "model.alias": Tool("切换模型别名", Alias, alias),
    "model.train": Tool("训练模型", Train, train),
    "model.evaluate": Tool("评估模型", Evaluate, evaluate),
    "model.predict": Tool("批量推理", Predict, predict),
    "workflow.route": Tool("按类别和阈值分流", Route, route),
    "ocr.read": Tool("读取图片文字", TextRead, read_text),
    "curation.suggest": Tool("生成照片类型候选", Curate, curate),
}

class ObjectFind(Selection):
    selector: str = "general-detector"
    labels: list[str] = Field(min_length=1)
    confidence: float = Field(default=0.4, ge=0, le=1)


class FileRoute(Selection):
    destination: str = "matched"
    execute: bool = False


def find_objects(c, **options):
    from .selection import find_objects
    return find_objects(c, **options)


def route_files(c, **options):
    from .selection import route_files
    return route_files(c, **options)


TOOLS.update({
    "selection.find": Tool("检测指定目标", ObjectFind, find_objects),
    "selection.files": Tool("分流到指定目录", FileRoute, route_files),
})

class CandidateArgs(Spec):
    limit: int = Field(default=1000,ge=1,le=1000)

class ClassifyArgs(Selection):
    hints_path: str | None = None
    model_dir: str | None = None

def sorting_candidates(c, **args):
    from .sorting import candidates
    return candidates(c, **args)

def sorting_quality(c, **args):
    from .sorting import quality
    return quality(c, **args)

def sorting_duplicates(c, **args):
    from .sorting import duplicates
    return duplicates(c, **args)

def sorting_classify(c, **args):
    from .sorting import classify
    return classify(c, **args)

TOOLS.update({
"sorting.candidates":Tool("读取待分拣图片", CandidateArgs, sorting_candidates),
"sorting.quality":Tool("检查画质并标记", Selection, sorting_quality),
"sorting.duplicates":Tool("重复图候选归组", Selection, sorting_duplicates),
"sorting.classify":Tool("自动照片类型归类", ClassifyArgs, sorting_classify),
})

class CategoryConfirm(Selection):
    category: str

class ClassifierTrain(Spec):
    version: str | None = None
    model_dir: str | None = None

class Reclassify(Selection):
    version: str
    min_confidence: float = Field(default=.7,ge=0,le=1)

def confirm_categories(c,**args):
    from .learning import confirm
    return confirm(c,**args)

def train_categories(c,**args):
    from .learning import train
    if not args.get("version"):   # 不填版本名：自动按时间起一个
        import time
        args["version"]="cls-"+time.strftime("%Y%m%d-%H%M%S")
    return train(c,**args)

def rerun_categories(c,**args):
    from .learning import reclassify
    return reclassify(c,**args)

TOOLS.update({
"curation.confirm":Tool("\u786e\u8ba4\u7167\u7247\u7c7b\u522b",CategoryConfirm,confirm_categories),
"curation.train":Tool("\u8bad\u7ec3\u7167\u7247\u5206\u7c7b\u5668",ClassifierTrain,train_categories),
"curation.reclassify":Tool("\u91cd\u65b0\u5f52\u7c7b\u7167\u7247",Reclassify,rerun_categories),
})
