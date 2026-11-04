import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import service
import vision

ROOT=Path(__file__).resolve().parent


class VisionTests(unittest.TestCase):
    def config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            for name in ('cuda.env','vision.env'):(path/name).write_text((ROOT/name).read_text())
            (path/'runtime.env').write_text((ROOT/'../../elastic-indexer-stack/k8s-manifests/embeddings/runtime.env').read_text())
            return vision.settings(path)

    def test_vision_has_independent_capacity_and_the_same_text_weights(self):
        config=self.config();image=vision.command(config);text=service.command(config)
        for flag,value in (('--publish','192.168.68.108:8082:8080'),('--parallel','1'),('--ctx-size','2048'),
                           ('--image-max-tokens','1120'),('--mmproj','/models/mmproj-embeddinggemma-2-Q8_0.gguf')):
            self.assertEqual(image[image.index(flag)+1],value)
        self.assertEqual(image[image.index('--model')+1],text[text.index('--model')+1])
        self.assertEqual(text[text.index('--parallel')+1],'64')
        self.assertIn('--read-only',image)
        self.assertNotIn('--api-key',image)

    def test_image_health_checks_real_multimodal_payload_and_wrong_vectors(self):
        config=self.config()
        def opener(request,timeout):
            body=json.loads(request.data)
            self.assertEqual(timeout,60)
            self.assertTrue(body['input'][0]['content'][0]['image_url']['url'].startswith('data:image/png;base64,'))
            self.assertEqual(request.get_header('Authorization'),'Bearer fixture-key')
            return io.BytesIO(json.dumps({'model':config['MODEL_ALIAS'],'data':[{'index':0,'embedding':[1.]+[0.]*767}]}).encode())
        with patch.object(service.KEY.__class__,'read_text',return_value='fixture-key'):
            self.assertTrue(vision.healthy(config,opener))
            self.assertFalse(vision.healthy(config,lambda *a,**kw:io.BytesIO(b'{}')))

    def test_bad_projector_or_overlapping_port_is_rejected(self):
        for content in ('VISION_PORT=8081\nVISION_PROJECTOR_SHA256=invalid\n',
                        'VISION_PORT=8082\nVISION_PROJECTOR_SHA256=invalid\n',
                        'VISION_PORT=8082\nVISION_PROJECTOR_SHA256='+('a'*64)+'\nVISION_PROJECTOR_FILE=../unsafe\n'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'vision.env').write_text(content)
                with patch.object(service,'settings',return_value={'BIND_PORT':'8081'}),self.assertRaises(ValueError):
                    vision.settings(root)
