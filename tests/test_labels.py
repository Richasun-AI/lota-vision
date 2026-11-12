"""Regression coverage for annotation admission and site-safe publication."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from lota import labels


def choice(value, name="coverage"):
    return {"type": "choices", "from_name": name, "value": {"choices": [value]}}


def box(name="gate"):
    return {"type": "rectanglelabels", "value": {
        "rectanglelabels": [name], "x": 10, "y": 20, "width": 40, "height": 50,
    }}


class AnnotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "telecom-inspection"
        self.rows = {}
        catalog = patch.object(labels, "get_image", side_effect=lambda path, image_id: self.rows[image_id])
        catalog.start()
        self.addCleanup(catalog.stop)

    def image(self, number, site):
        buffer = BytesIO()
        Image.new("RGB", (16, 12), (number, 1, 2)).save(buffer, format="PNG")
        content = buffer.getvalue()
        image_id = sha256(content).hexdigest()
        path = self.project / "raw" / image_id[:2] / f"{image_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.rows[image_id] = {"sha256": image_id, "raw_path": str(path), "site_id": site,
                               "checkpoint": "gate", "width": 16, "height": 12}
        return image_id

    def task(self, image_id, results, *, mode="detect", ground_truth=False):
        item, = labels.make_tasks(self.project, [image_id], media_base="http://localhost/media/{image_id}", task=mode)
        item["annotations"] = [{"result": results, "was_cancelled": False, "ground_truth": ground_truth}]
        return item

    def test_unlabeled_resend_does_not_overwrite_existing_annotation(self):
        image = self.image(7, "site-7")
        labels.import_annotations(self.project, [self.task(image, [box(), choice("Targets reviewed")])], ["gate"])
        resent = self.task(image, [])
        resent["annotations"] = []          # 送回复核后，Label Studio 里还没人标
        resent["predictions"] = [{"result": [box(), choice("Targets reviewed")]}]
        summary = labels.import_annotations(self.project, [resent], ["gate"])
        self.assertEqual(summary["counts"], {"kept_existing": 1})
        current, = labels.list_annotations(self.project)
        self.assertEqual(current["status"], "accepted")

    def test_predictions_and_conflicts_stay_out_of_the_dataset(self):
        positive, second, negative, predicted, conflict = [self.image(i, f"site-{i}") for i in range(1, 6)]
        unreviewed = self.task(predicted, [])
        unreviewed["annotations"] = []
        unreviewed["predictions"] = [{"result": [box()]}]
        disagree = self.task(conflict, [box(),choice("Targets reviewed")])
        disagree["annotations"].append({"result": [choice("No target")]})
        summary = labels.import_annotations(self.project, [
            self.task(positive, [box(),choice("Targets reviewed")]), self.task(second, [box(),choice("Targets reviewed")]),
            self.task(negative, [choice("No target")]), unreviewed, disagree,
        ], ["gate"])
        self.assertEqual(summary["counts"], {"accepted": 3, "unlabeled": 1, "needs_review": 1})
        manifest = labels.publish_dataset(self.project, "v1", ["gate"])
        entries = {item["image_id"]: item for item in manifest["images"]}
        self.assertEqual(set(entries), {positive, second, negative})
        dataset = self.project / "datasets" / "v1"
        negative_label = dataset / "labels" / entries[negative]["split"] / f"{negative}.txt"
        self.assertEqual(negative_label.read_text(), "")
        again = labels.publish_dataset(self.project, "v1", ["gate"])
        self.assertEqual(again["dataset_id"], manifest["dataset_id"])

    def test_classification_groups_and_class_ids_are_stable(self):
        tasks = []
        for i in range(6):
            name = ["z", "a"][i % 2]
            image_id = self.image(i, f"site-{i // 2}")
            tasks.append(self.task(image_id, [choice(name, "category"),choice("Category reviewed")], mode="classify"))
        labels.import_annotations(self.project, tasks, ["z", "a"], task="classify")
        manifest = labels.publish_dataset(self.project, "v1", ["z", "a"], task="classify",
                                          val_fraction=0.2, test_fraction=0.2)
        self.assertEqual(manifest["classes"], ["a", "z"])
        self.assertEqual(manifest["counts"], {"train": 2, "val": 2, "test": 2})
        for site in {"site-0", "site-1", "site-2"}:
            self.assertEqual(len({item["split"] for item in manifest["images"] if item["site_id"] == site}), 1)


    def test_task_isolation_and_explicit_review(self):
        image_id=self.image(20,"site-1")
        detect_dir=self.project/"annotation-tasks"/"devices"
        classify_dir=self.project/"annotation-tasks"/"photo-types"
        unreviewed=self.task(image_id,[box()])
        unreviewed["data"]["ailabel_project"]=labels._project_name(detect_dir)
        report=labels.import_annotations(detect_dir,[unreviewed],["gate"])
        self.assertEqual(report["accepted"],0)
        reviewed=self.task(image_id,[box(),choice("Targets reviewed")])
        reviewed["data"]["ailabel_project"]=labels._project_name(detect_dir)
        self.assertEqual(labels.import_annotations(detect_dir,[reviewed],["gate"])["accepted"],1)
        category=self.task(image_id,[choice("normal","category"),choice("Category reviewed")],mode="classify")
        category["data"]["ailabel_project"]=labels._project_name(classify_dir)
        self.assertEqual(labels.import_annotations(classify_dir,[category],["normal"],task="classify")["accepted"],1)
        self.assertEqual(labels.list_annotations(detect_dir)[0]["task"],"detect")
        self.assertEqual(labels.list_annotations(classify_dir)[0]["task"],"classify")
        negative=self.task(image_id,[])
        self.assertEqual(labels.import_annotations(self.project,[negative],["gate"])["accepted"],0)
        multiple=self.task(image_id,[{"type":"choices","from_name":"category","value":{"choices":["normal","rust"]}},choice("Category reviewed")],mode="classify")
        multiple["data"]["ailabel_project"]=labels._project_name(classify_dir)
        self.assertEqual(labels.import_annotations(classify_dir,[multiple],["normal","rust"],task="classify")["accepted"],0)

    def test_added_detection_class_requires_new_review(self):
        image_id=self.image(30,"site-1")
        labels.import_annotations(self.project,[self.task(image_id,[box(),choice("Targets reviewed")])],["gate"])
        samples,excluded=labels._collect_samples(self.project,["gate","camera"],"detect",True)
        self.assertEqual(samples,[])
        self.assertEqual(excluded["label_schema_changed"],1)

    def test_label_studio_retry_reconciles_remote_tasks(self):
        image_id = self.image(1, "site-1")
        remote = []

        class Client:
            base_url = "http://labelstudio"

            def list_tasks(self, project_id):
                return iter(remote)

            def import_tasks(self, project_id, tasks):
                remote.extend({"id": n, **task} for n, task in enumerate(tasks, 1))
                return {"task_ids": [1]}

        config = {"project_id": 7, "media_base": "http://localhost/media/"}
        with patch.object(labels.LabelStudioClient, "from_config", return_value=Client()):
            first = labels.push_tasks(self.project, config, [image_id], ["gate"])
            second = labels.push_tasks(self.project, config, [image_id], ["gate"])
        self.assertEqual((first["pushed"], second["pushed"], second["existing"]), (1, 0, 1))
        self.assertEqual(len(remote), 1)


if __name__ == "__main__":
    unittest.main()
