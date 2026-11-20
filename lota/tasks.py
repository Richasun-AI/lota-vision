"""Annotation task scope with shared original images and isolated outputs."""
from dataclasses import replace
from pathlib import Path
from .store import validate_id

def source_project(directory):
    directory=Path(directory)
    return directory.parent.parent if directory.parent.name=="annotation-tasks" else directory

def project_id_of(context):
    """子项目代码：标注任务里 context.directory 是 <子项目>/annotation-tasks/<任务>，不能直接取目录名"""
    return source_project(context.directory).name

def task_id_of(context):
    """当前在哪个标注任务里；不在任务里（历史默认任务）返回 None"""
    directory=Path(context.directory)
    return directory.name if directory.parent.name=="annotation-tasks" else None

def live_label_studio(context):
    """当前范围最新保存的 Label Studio 连接（同一任务里刚“连接 Label Studio”过也能读到）。
    标注任务只用自己绑定的项目，不借子项目的：两边标签表不同，借过去标注员会缺标签。"""
    config=context.engine.store.project(project_id_of(context))["config"]
    task_id=task_id_of(context)
    if task_id:
        return ((config.get("annotation_tasks") or {}).get(task_id) or {}).get("label_studio")
    return config.get("label_studio")

def require_label_studio(context):
    """取 Label Studio 连接，没有就给出能照着做的中文提示"""
    ls=live_label_studio(context)
    if ls and ls.get("project_id"):
        return ls
    task_id=task_id_of(context)
    if task_id:
        raise ValueError(f"标注任务“{task_id}”还没绑定 Label Studio 项目：先对这个任务运行“连接 Label Studio”"
                         "（会按任务的标签建项目），或在“管理标注任务”里填 Label Studio 地址和项目 ID")
    raise ValueError("这个子项目还没连接 Label Studio：先运行“连接 Label Studio”")

def task_context(context,task_id):
    validate_id(task_id)
    task=context.scenario.annotation_tasks.get(task_id)
    if task is None:
        raise ValueError("Unknown annotation task: "+task_id)
    root=source_project(context.directory)
    directory=root/"annotation-tasks"/task_id
    directory.mkdir(parents=True,exist_ok=True)
    config=context.scenario.model_copy(update={"task":task.task,"classes":task.classes,
        "require_site":task.require_site,"label_studio":task.label_studio})
    return replace(context,directory=directory,scenario=config)

def scoped(directory,scenario,task_id):
    if not task_id:return directory,scenario
    validate_id(task_id)
    task=scenario.annotation_tasks.get(task_id)
    if task is None:raise ValueError("Unknown annotation task")
    target=directory/"annotation-tasks"/task_id
    target.mkdir(parents=True,exist_ok=True)
    return target,task
