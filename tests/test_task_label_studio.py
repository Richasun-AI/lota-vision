"""标注任务里送 / 回收 Label Studio：按子项目代码查配置（不是任务目录名），只用任务自己的连接，任务标签表不被换掉"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lota import flow  # noqa: F401  注册 _with_live_ls 包装
from lota.store import Scenario, Store
from lota.tasks import live_label_studio, project_id_of, task_context, task_id_of
from lota.tools import TOOLS, Context

LS = {"base_url": "http://127.0.0.1:8081", "project_id": 7, "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}


def _config(task_ls=None, project_ls=None):
    task = {"name": "Telecom Site inspection", "task": "detect", "classes": ["cctv"]}
    if task_ls:
        task["label_studio"] = task_ls
    return Scenario.model_validate({"name": "Telecom inspection", "classes": ["mcb"], "label_studio": project_ls,
                                    "annotation_tasks": {"telecom-site-inspection": task}})


class TaskLabelStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "runtime")
        self.engine = SimpleNamespace(store=self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _task_ctx(self, config):
        self.store.save_project("telecom-inspection", config)
        base = Context(self.store.project_dir("telecom-inspection"), config, self.engine, "job", "step")
        return task_context(base, "telecom-site-inspection")

    def _push(self, ctx):
        seen = {}
        orig = TOOLS["labels.push"]
        # 包装里调用的是注册时的原函数；换成记录参数的假函数
        for cell in orig.function.__closure__ or ():
            if isinstance(cell.cell_contents, type(orig)):
                real = cell.cell_contents
                fake = type(orig)(real.title, real.model, lambda c, **kw: seen.setdefault("c", c))
                cell.cell_contents = fake
                try:
                    orig.function(ctx, image_ids=["a"])
                finally:
                    cell.cell_contents = real
                return seen.get("c")
        raise AssertionError("labels.push 没有被包装")

    def test_ids(self):
        ctx = self._task_ctx(_config())
        self.assertEqual(project_id_of(ctx), "telecom-inspection")
        self.assertEqual(task_id_of(ctx), "telecom-site-inspection")

    def test_task_without_its_own_connection_gets_clear_error(self):
        ctx = self._task_ctx(_config(project_ls=LS))   # 子项目有连接，任务没有：不借子项目的
        self.assertIsNone(live_label_studio(ctx))
        with self.assertRaisesRegex(ValueError, "标注任务“telecom-site-inspection”还没绑定 Label Studio"):
            self._push(ctx)

    def test_task_connection_is_used_and_classes_kept(self):
        ctx = self._task_ctx(_config(task_ls={**LS, "project_id": 9}, project_ls=LS))
        ctx.scenario = ctx.scenario.model_copy(update={"label_studio": None})   # 模拟任务开始时快照里还没有连接
        c = self._push(ctx)
        self.assertEqual(c.scenario.label_studio.project_id, 9)
        self.assertEqual(c.scenario.classes, ["cctv"])
        self.assertEqual(c.scenario.task, "detect")


if __name__ == "__main__":
    unittest.main()
