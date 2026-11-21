"""训练质量：更多 YOLO 指标、训练参数自动选（微调小学习率 / 自动调参结果）、自动调参、训练进度"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow, models


class MetricsTest(unittest.TestCase):
    def test_more_metrics(self):
        box = SimpleNamespace(map75=0.41, p=[0.9, 0.5], r=[0.6, 0.5], ap50=[0.8, 0.4], ap=[0.5, 0.2])
        results = SimpleNamespace(
            results_dict={"metrics/precision(B)": 0.7, "metrics/recall(B)": 0.55, "metrics/mAP50(B)": 0.6,
                          "metrics/mAP50-95(B)": 0.35, "fitness": 0.35},
            # ap_class_index 和 Ultralytics DetMetrics 一样挂在结果对象上
            box=box, ap_class_index=[0, 1], names={0: "weed", 1: "camera"},
            speed={"preprocess": 0.5, "inference": 6.2, "postprocess": 1.1})
        m = models._metric_dict(results)
        self.assertAlmostEqual(m["metrics/F1(B)"], 2 * 0.7 * 0.55 / 1.25)
        self.assertEqual(m["metrics/mAP75(B)"], 0.41)
        self.assertEqual(m["speed/inference"], 6.2)
        self.assertAlmostEqual(m["class/weed/f1"], 2 * 0.9 * 0.6 / 1.5)
        self.assertEqual(m["class/camera/ap"], 0.2)


class AutoOptionsTest(unittest.TestCase):
    def test_continue_uses_small_lr_and_keeps_user_values(self):
        with tempfile.TemporaryDirectory() as d:
            opts, notes = flow.auto_train_options(d, {"epochs": 60}, continuing=True)
            self.assertEqual((opts["optimizer"], opts["lr0"], opts["warmup_epochs"], opts["cos_lr"]), ("AdamW", 0.0005, 1.0, True))
            opts, _ = flow.auto_train_options(d, {"epochs": 60, "lr0": 0.002}, continuing=True)
            self.assertEqual((opts["lr0"], opts["optimizer"]), (0.002, "AdamW"))   # 手动填的不动，配上优化器
            self.assertTrue(notes)

    def test_fresh_training_lr_needs_optimizer(self):
        with tempfile.TemporaryDirectory() as d:
            opts, _ = flow.auto_train_options(d, {"lr0": 0.01}, continuing=False)
            self.assertEqual(opts["optimizer"], "SGD")              # optimizer=auto 会忽略 lr0
            opts, notes = flow.auto_train_options(d, {"epochs": 80}, continuing=False)
            self.assertNotIn("optimizer", opts)                      # 没调参：交给 Ultralytics 自动选
            self.assertIn("Ultralytics", notes[0])

    def test_tuned_hyp_applied(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / flow.TUNED_FILE).write_text(json.dumps({"hyp": {"lr0": 0.003, "mosaic": 0.6, "box": 9.0},
                                                               "optimizer": "AdamW", "at": "t", "dataset_version": "v1"}), encoding="utf-8")
            opts, _ = flow.auto_train_options(d, {"epochs": 80, "mosaic": 0.2}, continuing=False)
            self.assertEqual((opts["lr0"], opts["box"], opts["mosaic"], opts["optimizer"]), (0.003, 9.0, 0.2, "AdamW"))
            opts, _ = flow.auto_train_options(d, {"epochs": 40}, continuing=True)
            self.assertEqual((opts["lr0"], opts["box"]), (0.0005, 9.0))   # 接着训：调参的学习率不用，其余照用
            opts, _ = flow.auto_train_options(d, {"epochs": 80}, continuing=False, use_tuned=False)
            self.assertNotIn("box", opts)


class TuneTest(unittest.TestCase):
    def test_tune_reads_best_hyperparameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            w = root / "base.pt"
            w.write_bytes(b"weights")
            calls = {}

            class FakeYOLO:
                def tune(self, **kw):
                    calls.update(kw)
                    out = Path(kw["project"]) / kw["name"]
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "best_hyperparameters.yaml").write_text("lr0: 0.002\nmosaic: 0.7\nunknown_key: 3\n", encoding="utf-8")
                    (out / "tune_results.csv").write_text("fitness,lr0\n0.31,0.01\n0.42,0.002\n", encoding="utf-8")

            manifest = {"task": "detect", "classes": ["weed"], "dataset_id": "abc"}
            with mock.patch.object(models, "_dataset", lambda p, v: (manifest, root)), \
                    mock.patch.object(models, "_stage_dataset", lambda *a, **k: root / "data.yaml"), \
                    mock.patch.object(models, "_runtime_yolo", lambda *a: FakeYOLO()):
                out = models.tune(root, "v1", w, iterations=5, epochs=10, optimizer="AdamW", device="0")
                with self.assertRaises(ValueError):
                    models.tune(root, "v1", w, bogus=1)                 # 不认识的参数直接报错
            self.assertEqual(out["hyp"], {"lr0": 0.002, "mosaic": 0.7})
            self.assertEqual((out["tried"], out["best_fitness"]), (2, 0.42))
            self.assertEqual((calls["iterations"], calls["epochs"], calls["optimizer"]), (5, 10, "AdamW"))

    def test_tune_table_reads_ndjson(self):
        # Ultralytics 8.4 写 tune_results.ndjson（每行一组），不再写 csv
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tune"
            out.mkdir()
            (out / "tune_results.ndjson").write_text(
                '{"iteration": 1, "fitness": 0.26, "hyperparameters": {"lr0": 0.01}}\n'
                '{"iteration": 2, "fitness": 0.40, "hyperparameters": {"lr0": 0.0002}}\n\n', encoding="utf-8")
            self.assertEqual(models._tune_table(Path(tmp)), (2, 0.40))


class ProgressTest(unittest.TestCase):
    def test_progress_reads_strict_score_and_losses(self):
        from lota.hub import train_progress
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "runs" / "m1" / "x" / "train"
            run.mkdir(parents=True)
            (run / "results.csv").write_text(
                "epoch,time,train/box_loss,metrics/precision(B),metrics/recall(B),metrics/mAP50(B),metrics/mAP50-95(B),val/box_loss\n"
                "1,30.5,1.2,0.5,0.4,0.45,0.2,1.4\n", encoding="utf-8")
            g = train_progress(tmp)
            p = g["points"][0]
            self.assertEqual((p["map5095"], p["time"], p["train_box_loss"], p["val_box_loss"]), (0.2, 30.5, 1.2, 1.4))


if __name__ == "__main__":
    unittest.main()
