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


class EzvizTests(unittest.TestCase):
    def setUp(self):
        from lota import ezviz
        self.ez = ezviz
        ezviz._tokens.clear(); ezviz._urls.clear(); ezviz._decrypted.clear()
        self.calls = []
        self.replies = {}
        def fake(path, data, timeout=10):
            self.calls.append((path, dict(data)))
            r = self.replies.get(path)
            if isinstance(r, list):
                r = r.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        self._orig, ezviz._post = ezviz._post, fake

    def tearDown(self):
        self.ez._post = self._orig

    def ezcam(self, **kw):
        return cam(brand="ezviz", host="", ez_app_key="k", ez_app_secret="s3cret", ez_serial="j12345678", **kw)

    def test_display_url_and_masking(self):
        c = self.ezcam(ez_code="ABCDEF")
        self.assertEqual(build_url(c), "ezviz://J12345678/1?sub&flv")
        d = public(c)
        self.assertEqual((d["ez_app_secret"], d["ez_code"]), (MASK, MASK))
        self.assertNotIn("s3cret", str(d))

    def test_missing_fields_rejected_without_network(self):
        with self.assertRaises(ValueError):
            build_url(cam(brand="ezviz", host="", ez_app_key="k", ez_app_secret="", ez_serial="J12345678"))
        with self.assertRaises(ValueError):
            build_url(cam(brand="ezviz", host="", ez_app_key="k", ez_app_secret="s", ez_serial="bad serial"))
        self.assertEqual(self.calls, [])

    def test_live_url_cached_and_token_refresh(self):
        from lota.streams import resolve_url
        self.replies = {"/api/lapp/token/get": [{"accessToken": "at.1", "expireTime": (__import__("time").time() + 7 * 86400) * 1000},
                                                {"accessToken": "at.2", "expireTime": (__import__("time").time() + 7 * 86400) * 1000}],
                        "/api/lapp/v2/live/address/get": [self.ez.EzvizError("10002", "expired"), {"url": "https://x/live.flv"}]}
        c = self.ezcam(substream=False, channel=2)
        self.assertEqual(resolve_url(c), "https://x/live.flv")
        addr = [d for p, d in self.calls if p.endswith("address/get")]
        self.assertEqual(addr[-1]["accessToken"], "at.2")
        self.assertEqual((addr[-1]["deviceSerial"], addr[-1]["channelNo"], addr[-1]["quality"], addr[-1]["protocol"]), ("J12345678", 2, 1, 4))
        n = len(self.calls)
        self.assertEqual(resolve_url(c), "https://x/live.flv")   # 缓存，不再联网
        self.assertEqual(len(self.calls), n)

    def test_code_turns_off_encryption_once(self):
        import time
        self.replies = {"/api/lapp/token/get": {"accessToken": "at", "expireTime": (time.time() + 86400 * 7) * 1000},
                        "/api/lapp/device/encrypt/off": self.ez.EzvizError("60016", "already off"),
                        "/api/lapp/v2/live/address/get": {"url": "https://x/a.flv"}}
        c = self.ezcam(ez_code="ABCDEF")
        self.ez.live_url(c); self.ez.live_url(c, force=True)
        self.assertEqual(sum(p.endswith("encrypt/off") for p, _ in self.calls), 1)

    def test_error_message_is_chinese(self):
        self.assertIn("不在线", str(self.ez.EzvizError("20007", "x")))


if __name__ == "__main__":
    unittest.main()
