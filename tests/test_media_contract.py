"""The media naming contract.

Existing images and recordings are found by name. If any of these derivations
changes, files already on disk stop matching and are generated again at cost.
A failure here means the change must be reverted, not that the expected values
should be updated.
"""

import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from flashcards import assets, notes, paths, util
from flashcards.settings import AudioSettings, ImageSettings, settings


class MediaContract(unittest.TestCase):
    def test_baseline_settings_that_filenames_depend_on(self):
        # Code defaults, not settings.toml values: audio_filename compares against them.
        self.assertEqual(
            asdict(AudioSettings()),
            {
                "voice_id": "HuK8QKF35exsCh2e7fLT",
                "model": "eleven_multilingual_v2",
                "format": "mp3_44100_128",
                "language": "it",
                "stability": 0.5,
                "similarity_boost": 1.0,
                "style": 1.0,
                "speed": 0.7,
                "compressed_bitrate": "48k",
            },
        )
        self.assertEqual((ImageSettings().compressed_max_px, ImageSettings().compressed_quality), (512, 75))

    def test_audio_names_at_baseline(self):
        with patch.object(util_settings(), "settings", replace(settings, audio=AudioSettings())):
            for text, name in (
                ("la casa", "25f2381de6275a463d18baa7dd8766a2.mp3"),
                ("sono andato", "469a0b5918d70f8828be64ef12c113cb.mp3"),
                (" perché ", "9ec9c29be21ecf2943988d3dc8ac5e69.mp3"),
            ):
                self.assertEqual(util.audio_filename(text), name)
                self.assertEqual(util.compressed_audio_name(name), name)

    def test_changed_voice_or_bitrate_gets_a_different_name(self):
        changed = replace(settings, audio=replace(AudioSettings(), speed=0.8, compressed_bitrate="64k"))
        with patch.object(util_settings(), "settings", changed):
            name = util.audio_filename("la casa")
            self.assertNotEqual(name, "25f2381de6275a463d18baa7dd8766a2.mp3")
            self.assertEqual(util.compressed_audio_name("x.mp3"), "x.64k.mp3")

    def test_image_names(self):
        with patch.object(util_settings(), "settings", replace(settings, images=ImageSettings())):
            self.assertEqual(util.image_filename("la casa", "png"), "25f2381de6275a463d18baa7dd8766a2.png")
            self.assertEqual(util.image_filename("la casa", "jpg"), "25f2381de6275a463d18baa7dd8766a2.jpg")
            self.assertEqual(util.image_filename(" la casa ", "png"), util.image_filename("la casa", "png"))
        other = replace(
            settings, images=replace(ImageSettings(), compressed_max_px=256, compressed_quality=60)
        )
        with patch.object(util_settings(), "settings", other):
            self.assertEqual(util.image_filename("la casa", "png"), "25f2381de6275a463d18baa7dd8766a2.png")
            self.assertEqual(
                util.image_filename("la casa", "jpg"), "25f2381de6275a463d18baa7dd8766a2.256q60.jpg"
            )

    def test_spoken_text(self):
        for shown, spoken in (
            ("sono andato/a", "sono andato"),
            ("il/la collega", "il collega"),
            ("bello / bella", "bello"),
            ("Come ti chiami…", "Come ti chiami"),
            ("vado ...", "vado"),
        ):
            self.assertEqual(notes._spoken(shown), spoken)

    def test_lookup_prefers_compressed_then_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = {
                name: root / name for name in ("images", "images_compressed", "audio", "audio_compressed")
            }
            for d in dirs.values():
                d.mkdir()
            with (
                patch.object(paths, "IMAGE_DIR", dirs["images"]),
                patch.object(paths, "IMAGE_DIR_COMPRESSED", dirs["images_compressed"]),
                patch.object(paths, "AUDIO_DIR", dirs["audio"]),
                patch.object(paths, "AUDIO_DIR_COMPRESSED", dirs["audio_compressed"]),
                patch.object(util_settings(), "settings", replace(settings, images=ImageSettings())),
            ):
                png = dirs["images"] / "25f2381de6275a463d18baa7dd8766a2.png"
                Image.new("RGB", (4, 4)).save(png)
                self.assertEqual(assets.image_for("la casa"), png)
                jpg = dirs["images_compressed"] / "25f2381de6275a463d18baa7dd8766a2.jpg"
                Image.new("RGB", (4, 4)).save(jpg)
                self.assertEqual(assets.image_for("la casa"), jpg)
                (dirs["audio"] / "a.mp3").write_bytes(b"x")
                self.assertEqual(assets.locate("a.mp3"), dirs["audio"] / "a.mp3")
                (dirs["audio_compressed"] / "a.mp3").write_bytes(b"y")
                self.assertEqual(assets.locate("a.mp3"), dirs["audio_compressed"] / "a.mp3")


def util_settings():
    """The module whose ``settings`` the naming functions read."""
    import flashcards.settings as module

    return module


if __name__ == "__main__":
    unittest.main()
