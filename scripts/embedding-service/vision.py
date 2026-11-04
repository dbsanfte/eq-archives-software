"""Authenticated, bounded Gemma image embeddings on the CUDA host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

import service

NAME='eqarchives-gemma-vision'
# A valid transparent PNG, used only to verify real image inference.
PIXEL='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNIUAAAAASUVORK5CYII='


def settings(root=service.ROOT):
    config=service.settings(root)
    for line in (root/'vision.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key,value=line.split('=',1);config[key]=value
    if not 1024<=int(config['VISION_PORT'])<=65535 or config['VISION_PORT']==config['BIND_PORT']:
        raise ValueError('Use an independent private image service port')
    if not re.fullmatch('[a-f0-9]{64}',config['VISION_PROJECTOR_SHA256']):
        raise ValueError('An immutable vision projector hash is required')
    if Path(config['VISION_PROJECTOR_FILE']).name!=config['VISION_PROJECTOR_FILE']:
        raise ValueError('Invalid projector filename')
    return config


def command(config):
    result=service.command(config)
    for flag,value in [('--name',NAME),('--publish',f"{config['BIND_ADDRESS']}:{config['VISION_PORT']}:8080"),
                       ('--ctx-size','2048'),('--parallel','1'),('--kv-unified-per-slot','2048'),
                       ('--batch-size','2048'),('--ubatch-size','2048')]:
        result[result.index(flag)+1]=value
    result.extend(['--mmproj','/models/'+config['VISION_PROJECTOR_FILE'],
                   '--image-min-tokens','70','--image-max-tokens','1120'])
    return result


def healthy(config,opener=urllib.request.urlopen):
    try:
        payload={'model':config['MODEL_ALIAS'],'input':[{'content':[{'type':'image_url',
                 'image_url':{'url':'data:image/png;base64,'+PIXEL}}]}],'encoding_format':'float'}
        request=urllib.request.Request(f"http://{config['BIND_ADDRESS']}:{config['VISION_PORT']}/v1/embeddings",
            data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',
            'Authorization':'Bearer '+service.KEY.read_text().strip()})
        with opener(request,timeout=60) as response:
            from benchmark import vectors
            vectors(json.load(response),1,config['MODEL_ALIAS'])
        return True
    except Exception:
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','health'))
    args=parser.parse_args();config=settings()
    if args.action=='start':
        for file,expected in ((config['MODEL_FILE'],config['MODEL_SHA256']),
                              (config['VISION_PROJECTOR_FILE'],config['VISION_PROJECTOR_SHA256'])):
            with (service.MODEL_ROOT/file).open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
                    raise ValueError('Image embedding model checksum mismatch')
        subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        os.execvp('docker',command(config))
    failures=Path('/run/eqarchives-vision-embeddings/health-failures')
    if not failures.parent.is_dir():
        print('Image embedding service has not started',flush=True)
        return 1
    if healthy(config):
        failures.write_text('0')
        return 0
    count=int(failures.read_text())+1 if failures.exists() else 1
    failures.write_text(str(count))
    print(f'Image embedding inference unavailable ({count}/3); upstream details omitted',flush=True)
    if count>=3:
        subprocess.run(['systemctl','try-restart','eqarchives-vision-embeddings.service'],check=True)
        failures.write_text('0')
    return 1


if __name__=='__main__':
    raise SystemExit(main())
