"""Tests for fail-closed output handling in the approved style burner."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from subtitle.styles import burn_styles


class BurnStylesOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / 'synth-source.mp4'
        self.script = self.root / 'script.txt'
        self.mfa = self.root / 'words.json'
        self.script.write_text('สวัสดี\n', encoding='utf-8')
        self.mfa.write_text(
            json.dumps({'words': [{'word': 'สวัสดี', 'start': 0.0, 'end': 3.0}]},
                       ensure_ascii=False),
            encoding='utf-8',
        )

    def invoke_main(self, out_path: Path, *, overwrite: bool = False) -> None:
        argv = [
            'burn_styles.py',
            '--style', 'minimal',
            '--video', str(self.video),
            '--script', str(self.script),
            '--mfa', str(self.mfa),
            '--out', str(out_path),
            '--fps', '1',
        ]
        if overwrite:
            argv.append('--overwrite')
        with patch.object(sys, 'argv', argv):
            burn_styles.main()

    def test_existing_output_is_refused_and_unchanged(self) -> None:
        out_path = self.root / 'existing.mp4'
        original = b'existing clip bytes'
        out_path.write_bytes(original)

        with self.assertRaises(SystemExit) as raised:
            self.invoke_main(out_path)

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn('already exists', str(raised.exception))
        self.assertEqual(out_path.read_bytes(), original)

    def test_existing_proto_json_is_refused_without_writing(self) -> None:
        out_path = self.root / 'new-output.mp4'
        proto_path = Path(str(out_path) + '.proto.json')
        original = b'existing proto bytes'
        proto_path.write_bytes(original)

        with self.assertRaises(SystemExit) as raised:
            self.invoke_main(out_path)

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn('already exists', str(raised.exception))
        self.assertFalse(out_path.exists())
        self.assertEqual(proto_path.read_bytes(), original)

    @unittest.skipUnless(
        shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None,
        'FFmpeg and ffprobe must be installed to run the overwrite render test',
    )
    def test_existing_output_is_replaced_when_overwrite_is_given(self) -> None:
        subprocess.run(
            [
                'ffmpeg', '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=44100',
                '-t', '3.0',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                str(self.video),
            ],
            check=True,
            capture_output=True,
        )
        out_path = self.root / 'existing.mp4'
        proto_path = Path(str(out_path) + '.proto.json')
        old_output = b'old output bytes'
        old_proto = b'old proto bytes'
        out_path.write_bytes(old_output)
        proto_path.write_bytes(old_proto)

        real_mkdtemp = tempfile.mkdtemp

        def make_test_temp_dir(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=str(self.root)))
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
            return str(path)

        with patch('subtitle.styles.burn_styles.tempfile.mkdtemp',
                   side_effect=make_test_temp_dir):
            self.invoke_main(out_path, overwrite=True)

        self.assertTrue(out_path.is_file())
        self.assertGreater(out_path.stat().st_size, 0)
        self.assertNotEqual(out_path.read_bytes(), old_output)
        self.assertEqual(json.loads(proto_path.read_text(encoding='utf-8'))['style'], 'minimal')
        self.assertNotEqual(proto_path.read_bytes(), old_proto)


if __name__ == '__main__':
    unittest.main()
