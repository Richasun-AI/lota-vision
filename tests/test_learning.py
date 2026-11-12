import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from lota import learning,sorting


class LearningLoopTest(unittest.TestCase):
    def test_confirm_train_reclassify_preserves_human_and_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            c=SimpleNamespace(directory=root,scenario=SimpleNamespace(categories={
                "gate":SimpleNamespace(enabled=True),"meter":SimpleNamespace(enabled=True)}))
            ids=[str(i).zfill(64) for i in range(20)]
            rows={key:{"site_id":"site-"+str(i//2)} for i,key in enumerate(ids)}
            with patch.object(learning,"get_image",side_effect=lambda root,key:rows[key]):
                learning.confirm(c,ids[::2],"gate")
                learning.confirm(c,ids[1::2],"meter")
                def feature_values(c,keys,model):
                    return np.array([[4.,-4.] if int(key)%2==0 else [-4.,4.] for key in keys]),"stable-features"
                with patch.object(learning,"features",side_effect=feature_values):
                    result=learning.train(c,"v1","/fixture-model")
                    self.assertFalse(set(result["report"]["train_sites"])&set(result["report"]["validation_sites"]))
                    self.assertEqual(result["report"]["validation_accuracy"],1)
                    learning.confirm(c,[ids[0]],"meter")
                    output=learning.reclassify(c,[ids[0],ids[1]],"v1")
                    self.assertEqual(output["items"][0]["category"],"meter")
                    self.assertEqual(output["items"][0]["provenance"],"human")
                    self.assertTrue((root/"classifiers/active.json").is_file())
                    for key in ids:rows[key]={"duplicate_group":key}
                    ungrouped=learning.train(c,"v2","/fixture-model")
                    self.assertEqual(ungrouped["report"]["grouping"],"image_or_duplicate_group")
                with patch.object(sorting,"get_image",side_effect=lambda root,key:rows[key]):
                    final=sorting.results(root)
                    self.assertEqual(final["categories"]["meter"],20//2+1)


if __name__=="__main__":
    unittest.main()
