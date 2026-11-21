"""长任务进度：工具上报 → 任务表 progress；提前结束请求；自动调参一组一组跑、成绩不再提高就停"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lota import models, progress
from lota.store import Store


class ReporterTest(unittest.TestCase):
    def test_report_without_job_is_noop(self):
        progress.report(1, 10, "张", "x")          # 不在任务里：什么都不做，也不报错
        self.assertFalse(progress.stop_requested())

    def test_report_throttles_and_binds(self):
        seen = []
        r = progress.Reporter(seen.append, min_interval=60)
        token = progress.bind(r)
        try:
            progress.report(1, 10, "张", "first")
            progress.report(2, 10, "张", "throttled")
            progress.report(3, 10, "张", "forced", force=True)
        finally:
            progress.unbind(token)
        self.assertEqual([d["message"] for d in seen], ["first", "forced"])
        self.assertEqual((seen[-1]["done"], seen[-1]["total"], seen[-1]["unit"]), (3, 10, "张"))

    def test_watch_polls_in_background(self):
        seen = []
        token = progress.bind(progress.Reporter(seen.append))
        try:
            with progress.watch(lambda: {"done": 5, "total": 10, "message": "w"}, interval=0.05):
                time.sleep(0.2)
        finally:
            progress.unbind(token)
        self.assertTrue(seen and seen[-1]["message"] == "w")


class StoreProgressTest(unittest.TestCase):
    def test_progress_and_stop_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            now = time.time()
            with store.connection() as db:
                cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            self.assertTrue({"progress", "stop_requested"} <= cols)
            with store.connection() as db:
                db.execute("""INSERT INTO jobs(id,project_id,revision,kind,spec,inputs,outputs,status,worker,created,updated)
                    VALUES('j','p',1,'model.tune','{}','{}','{}','running','w',?,?)""", (now, now))
            store.set_progress("j", "w", {"done": 3, "total": 30, "message": "m"})
            store.set_progress("j", "other-worker", {"done": 99})        # 不是自己的任务：不写
            with store.connection() as db:
                row = db.execute("SELECT progress FROM jobs WHERE id='j'").fetchone()
            self.assertEqual(json.loads(row[0])["done"], 3)
            self.assertFalse(store.stop_requested("j"))
            with mock.patch.object(store, "job", lambda p, j: {"id": j}):
                store.change_job("p", "j", "stop")
            self.assertTrue(store.stop_requested("j"))

    def test_stop_only_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            now = time.time()
            with store.connection() as db:
                db.execute("""INSERT INTO jobs(id,project_id,revision,kind,spec,inputs,outputs,status,created,updated)
                    VALUES('q','p',1,'model.tune','{}','{}','{}','queued',?,?)""", (now, now))
            with self.assertRaises(ValueError):
                store.change_job("p", "q", "stop")


class TuneEarlyStopTest(unittest.TestCase):
    def test_plateau(self):
        self.assertFalse(models.tune_plateau([0.3] * 9, 8))                  # 不到 10 组不停
        self.assertTrue(models.tune_plateau([0.2, 0.4] + [0.39] * 8, 8))     # 最近 8 组没超过 0.4
        self.assertFalse(models.tune_plateau([0.2, 0.4] + [0.39] * 7 + [0.41], 8))
        self.assertFalse(models.tune_plateau([0.3] * 20, 0))                  # 0 = 试满

    def _run(self, fits, stop_after=None, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        w = root / "base.pt"
        w.write_bytes(b"weights")
        calls = []

        class FakeYOLO:
            def tune(self, **a):
                calls.append(a["iterations"])
                out = Path(a["project"]) / a["name"]
                out.mkdir(parents=True, exist_ok=True)
                f = out / "tune_results.ndjson"
                n = len(f.read_text().splitlines()) if f.is_file() else 0
                with f.open("a") as fh:
                    for i in range(n, a["iterations"]):
                        fh.write(json.dumps({"iteration": i + 1, "fitness": fits[i]}) + "\n")
                (out / "best_hyperparameters.yaml").write_text("lr0: 0.002\n", encoding="utf-8")

        manifest = {"task": "detect", "classes": ["weed"], "dataset_id": "abc"}
        stop = mock.patch.object(progress, "stop_requested", lambda: stop_after is not None and len(calls) >= stop_after)
        with mock.patch.object(models, "_dataset", lambda p, v: (manifest, root)), \
                mock.patch.object(models, "_stage_dataset", lambda *a, **k: root / "data.yaml"), \
                mock.patch.object(models, "_runtime_yolo", lambda *a: FakeYOLO()), stop:
            return models.tune(root, "v1", w, epochs=10, **kw), calls

    def test_stops_when_no_improvement(self):
        out, calls = self._run([0.2, 0.4] + [0.39] * 28, iterations=30, stop_patience=8)
        self.assertEqual((out["tried"], out["best_fitness"]), (10, 0.4))
        self.assertIn("提前结束", out["stopped_early"])
        self.assertEqual(calls, list(range(1, 11)))          # 每次多试一组（Tuner 从记录接着试）

    def test_user_stop_and_full_run(self):
        out, _ = self._run([0.1 * i for i in range(1, 31)], stop_after=4, iterations=30)
        self.assertEqual(out["tried"], 4)
        self.assertIn("按要求", out["stopped_early"])
        out, _ = self._run([0.1] * 5, iterations=5, stop_patience=0)
        self.assertEqual((out["tried"], out["stopped_early"]), (5, ""))


class TrainProgressTest(unittest.TestCase):
    def test_train_and_tune_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            self.assertEqual(models._train_progress(run / "none.csv", 50)["done"], 0)
            (run / "train").mkdir()
            (run / "train" / "results.csv").write_text(
                " epoch, metrics/mAP50-95(B)\n 1, 0.2\n 2, 0.3\n", encoding="utf-8")
            p = models._train_progress(run / "train" / "results.csv", 50)
            self.assertEqual((p["done"], p["total"]), (2, 50))
            self.assertIn("0.300", p["message"])
            (run / "tune").mkdir()
            (run / "tune" / "tune_results.ndjson").write_text('{"fitness": 0.3}\n{"fitness": 0.35}\n', encoding="utf-8")
            t = models._tune_progress(run, 30, 10)
            self.assertAlmostEqual(t["done"], 2.2)               # 2 组试完 + 第 3 组训到第 2/10 轮
            self.assertTrue(t["stoppable"])


if __name__ == "__main__":
    unittest.main()
