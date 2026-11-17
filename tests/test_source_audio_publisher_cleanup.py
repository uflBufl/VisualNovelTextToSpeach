import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer

import psutil

from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
    Reverse1999GameImporter,
)


class SourceAudioPublisherCleanupTests(unittest.TestCase):
    def test_finished_and_failed_publishers_remove_scratch_and_preserve_cache(self):
        script = """
import json, os, sys, tempfile
from pathlib import Path
directory = Path(tempfile.mkdtemp(prefix='r1999-source-duration-'))
(directory / 'voice.wem').write_bytes(b'WEM')
(directory / 'voice.wem.wav').write_bytes(b'WAV')
print(json.dumps({'scratch': str(directory.parent), 'env': {
    key: os.environ[key] for key in ('TMPDIR', 'TEMP', 'TMP', 'EXPLICIT')
}}), flush=True)
sys.exit(int(sys.argv[1]))
"""
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "analysis-cache"
            cache.mkdir()
            cached = cache / "ready.json"
            cached.write_text("valid cache")
            unrelated = root / "unrelated.wav"
            unrelated.write_bytes(b"keep")
            environment = dict(os.environ, EXPLICIT="preserved")
            original_environment = environment.copy()
            for exit_code in (0, 3):
                processes = []

                def start(*args, **kwargs):
                    processes.append(Path(kwargs["env"]["TMPDIR"]))
                    return subprocess.Popen(*args, **kwargs)

                importer = Reverse1999GameImporter(
                    output_root=root, popen_factory=start
                )
                with self.subTest(exit_code=exit_code):
                    if exit_code:
                        with self.assertRaises(GameContentImportError):
                            importer._run_source_audio_publisher(
                                (sys.executable, "-c", script, str(exit_code)),
                                None,
                                None,
                                environment=environment,
                            )
                    else:
                        stdout, _ = importer._run_source_audio_publisher(
                            (sys.executable, "-c", script, "0"),
                            None,
                            None,
                            environment=environment,
                        )
                        result = json.loads(stdout)
                        self.assertEqual(result["env"]["EXPLICIT"], "preserved")
                        for key in ("TMPDIR", "TEMP", "TMP"):
                            self.assertEqual(result["env"][key], result["scratch"])
                    self.assertEqual(len(processes), 1)
                    self.assertFalse(processes[0].exists())
                    self.assertEqual(cached.read_text(), "valid cache")
                    self.assertEqual(unrelated.read_bytes(), b"keep")
                    self.assertEqual(environment, original_environment)

    def test_cancel_and_progress_error_drain_decoder_before_scratch_cleanup(self):
        decoder_script = """
import json, os, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
(directory / 'voice.wem.wav').write_bytes(b'WAV')
Path(sys.argv[2]).write_text(json.dumps({
    'decoder_pid': os.getpid(), 'scratch': str(directory.parent)
}))
print('R1999_PROGRESS '+json.dumps({
    'schema':'r1999.source-audio-progress','schema_version':1,
    'stage':'duration','processed':0,'total':1,'state':'analyzing'
}), flush=True)
time.sleep(30)
"""
        worker_script = """
import subprocess, sys, tempfile
from pathlib import Path
directory = Path(tempfile.mkdtemp(prefix='r1999-source-duration-'))
(directory / 'voice.wem').write_bytes(b'WEM')
subprocess.run([sys.executable, '-c', sys.argv[1], str(directory), sys.argv[2]], check=True)
"""
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for fail in (False, True):
                with self.subTest(progress_error=fail):
                    marker = root / "decoder.json"
                    cancelled = Event()
                    timeout = Timer(5, cancelled.set)
                    self.addCleanup(timeout.cancel)
                    timeout.start()

                    def progress(_event):
                        info = json.loads(marker.read_text())
                        scratch = Path(info["scratch"])
                        self.assertTrue(list(scratch.rglob("*.wem.wav")))
                        if fail:
                            raise RuntimeError("progress failed")
                        cancelled.set()

                    importer = Reverse1999GameImporter(output_root=root)
                    error = RuntimeError if fail else GameContentImportCancelled
                    with self.assertRaises(error) as raised:
                        importer._run_source_audio_publisher(
                            (
                                sys.executable,
                                "-c",
                                worker_script,
                                decoder_script,
                                str(marker),
                            ),
                            cancelled,
                            progress,
                        )
                    timeout.cancel()
                    if fail:
                        self.assertEqual(str(raised.exception), "progress failed")
                    info = json.loads(marker.read_text())
                    self.assertFalse(Path(info["scratch"]).exists())
                    try:
                        decoder = psutil.Process(info["decoder_pid"])
                    except psutil.NoSuchProcess:
                        pass
                    else:
                        self.assertEqual(decoder.status(), psutil.STATUS_ZOMBIE)

                    stdout, _ = importer._run_source_audio_publisher(
                        (sys.executable, "-c", "print('retry ready')"), None, None
                    )
                    self.assertEqual(stdout.strip(), "retry ready")
