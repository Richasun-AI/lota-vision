"""Annotation task scope with shared original images and isolated outputs."""
from dataclasses import replace
from pathlib import Path
from .store import validate_id

def source_project(directory):
    directory=Path(directory)
    return directory.parent.parent if directory.parent.name=="annotation-tasks" else directory

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
