import unittest

from pydantic import ValidationError

from lota.streams import MASK, Camera, build_url, public, redact


def cam(**kw):
    base = {"id": "gate-1", "name": "大门", "host": "192.168.1.64", "username": "admin", "password": "p@ss:w/rd"}
    return Camera.model_validate({**base, **kw})


class StreamUrlTests(unittest.TestCase):
    def test_hikvision_main_and_sub(self):
        self.assertEqual(build_url(cam(brand="hikvision", substream=False)),
                         "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.64:554/Streaming/Channels/101")
        self.assertEqual(build_url(cam(brand="hikvision", channel=3, substream=True)),
                         "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.64:554/Streaming/Channels/302")

    def test_dahua_and_uniview(self):
        self.assertEqual(build_url(cam(brand="dahua", channel=2, substream=False)),
                         "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.64:554/cam/realmonitor?channel=2&subtype=0")
        self.assertEqual(build_url(cam(brand="uniview", substream=True, port=8554)),
                         "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.64:8554/unicast/c1/s1/live")

    def test_url_brand_adds_credentials_and_checks_scheme(self):
        self.assertEqual(build_url(cam(brand="url", url="rtsp://10.0.0.5:554/live", username="u", password="a b")),
                         "rtsp://u:a%20b@10.0.0.5:554/live")
        self.assertEqual(build_url(cam(brand="url", url="rtsp://x:y@10.0.0.5/live")), "rtsp://x:y@10.0.0.5/live")
        for bad in ("file:///etc/passwd", "10.0.0.5/live", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                build_url(cam(brand="url", url=bad))

    def test_host_required_and_validated(self):
        with self.assertRaises(ValueError):
            build_url(cam(host=""))
        with self.assertRaises(ValueError):
            build_url(cam(host="1.2.3.4/evil"))

    def test_password_never_leaks(self):
        c = cam(brand="hikvision")
        self.assertNotIn("p@ss", redact(build_url(c)))
        self.assertIn(MASK, redact(build_url(c)))
        d = public(c)
        self.assertEqual(d["password"], MASK)
        self.assertNotIn("p%40ss", d["stream_url"])
        d = public(cam(brand="url", url="rtsp://x:secret@10.0.0.5/live", username="", password=""))
        self.assertNotIn("secret", d["url"] + d["stream_url"])

    def test_id_pattern(self):
        with self.assertRaises(ValidationError):
            cam(id="Gate 1")
        with self.assertRaises(ValidationError):
            cam(id="../x")


if __name__ == "__main__":
    unittest.main()
