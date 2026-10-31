import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import service

ROOT = Path(__file__).resolve().parent

class ServiceTests(unittest.TestCase):
    def config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root/'cuda.env').write_text((ROOT/'cuda.env').read_text())
            (root/'runtime.env').write_text((ROOT/'../../elastic-indexer-stack/k8s-manifests/embeddings/runtime.env').read_text())
            return service.settings(root)

    def test_pins_capacity_security_and_private_bind(self):
        config = self.config()
        command = service.command(config)
        self.assertIn('--read-only', command)
        self.assertEqual(command[command.index('--publish')+1], '192.168.50.193:8081:8080')
        for flag, value in [('--ubatch-size','512'),('--parallel','64'),('--ctx-size','32768'),
                            ('--pooling','mean'),('--device','CUDA0')]:
            self.assertIn([flag, value], [command[i:i+2] for i in range(len(command)-1)])
        self.assertTrue(any('@sha256:' in value for value in command))
        self.assertNotIn('--privileged', command)
        self.assertNotIn('--api-key', command)

    def test_health_checks_real_vectors_and_hides_errors(self):
        config = self.config()
        payload = {'model': config['MODEL_ALIAS'], 'data': [{'index': 0, 'embedding':[1.]+[0.]*767}]}
        with patch.object(service.KEY.__class__, 'read_text', return_value='fixture-key'):
            def opener(request, timeout):
                self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-key')
                self.assertEqual(timeout, 15)
                return io.BytesIO(json.dumps(payload).encode())
            self.assertTrue(service.healthy(config, opener))
            payload['data'][0]['embedding'] = [0.]*768
            self.assertFalse(service.healthy(config, opener))
            self.assertFalse(service.healthy(config, lambda *a, **kw: (_ for _ in ()).throw(OSError('private error'))))


class BootRecoveryTests(unittest.TestCase):
    def test_persistence_precedes_cdi_and_installer_probes(self):
        persistent = (ROOT/'nvidia-persistenced.conf').read_text()
        cdi = (ROOT/'nvidia-cdi-persistence.conf').read_text()
        self.assertIn('--persistence-mode', persistent)
        self.assertNotIn('--no-persistence-mode', persistent)
        self.assertIn('StopWhenUnneeded=false', persistent)
        self.assertIn('After=nvidia-persistenced.service', cdi)
        script = (ROOT/'install.sh').read_text()
        self.assertLess(script.index('systemctl start nvidia-persistenced.service'), script.index('nvidia-smi -pm 1'))
        self.assertLess(script.index('nvidia-smi -pm 1'), script.index('systemctl enable --now docker'))

    def test_manual_health_check_before_service_start_is_clear_and_does_not_restart(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'absent'
            output = io.StringIO()
            with patch.object(service, 'settings', return_value={}), \
                 patch('sys.argv', ['service.py','health']), \
                 patch.object(service, 'Path', side_effect=lambda *a: root/'health-failures'), \
                 patch.object(service.subprocess, 'run') as run, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(service.main(), 1)
            self.assertIn('has not started', output.getvalue())
            run.assert_not_called()
            self.assertFalse(root.exists())

if __name__ == '__main__': unittest.main()
