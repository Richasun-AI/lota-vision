import unittest

from lota.labels import _class_name


class LabelNameTest(unittest.TestCase):
    def test_label_studio_names_map_to_class_codes(self):
        classes = ["rust", "grounding_bar"]
        self.assertEqual(_class_name(["rust"], classes), "rust")
        self.assertEqual(_class_name(["grounding bar"], classes), "grounding_bar")
        self.assertEqual(_class_name(["Grounding-Bar"], classes), "grounding_bar")
        self.assertIsNone(_class_name(["meter"], classes))
        self.assertIsNone(_class_name(["rust", "grounding bar"], classes))
        self.assertIsNone(_class_name("rust", classes))


if __name__ == "__main__":
    unittest.main()
