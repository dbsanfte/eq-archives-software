import base64
import hashlib
import io
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from indexer import gemma, image_embeddings as visual


def raster(size=(100,80),format='PNG',mode='RGBA'):
    output=io.BytesIO()
    image=Image.new(mode,size,(30,60,120,0) if mode=='RGBA' else 'blue')
    if mode=='RGBA':image.putpixel((size[0]//2,size[1]//2),(30,60,120,255))
    image.save(output,format=format)
    return output.getvalue()


def test_normalized_raster_preserves_identity_aspect_alpha_and_source_provenance():
    raw=raster((2000,1000));image=visual.prepare_image(raw)
    assert image.source_sha256==hashlib.sha256(raw).hexdigest()
    assert (image.width,image.height)==(2000,1000)
    encoded=base64.b64decode(image.data_url.split(',',1)[1])
    with Image.open(io.BytesIO(encoded)) as decoded:
        assert decoded.size==(1600,800)
        assert decoded.getpixel((0,0))==(255,255,255)
    assert image.content()['content'][0]['type']=='image_url'
    assert image.provenance()['frame']==0
    assert image.provenance()['version']==visual.VERSION


@pytest.mark.parametrize('format',['PNG','JPEG','GIF','WEBP','BMP','TIFF'])
def test_standard_archived_rasters_can_be_embedded_without_paid_captioning(format):
    image=visual.prepare_image(raster(format=format,mode='RGB'))
    assert image.data_url.startswith('data:image/png;base64,')


def test_animated_gif_uses_an_explicit_first_frame():
    output=io.BytesIO()
    first=Image.new('RGB',(32,32),'red');second=Image.new('RGB',(32,32),'blue')
    first.save(output,format='GIF',save_all=True,append_images=[second])
    image=visual.prepare_image(output.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(image.data_url.split(',',1)[1]))) as decoded:
        assert decoded.getpixel((0,0))==(255,0,0)


def test_bad_large_and_decorative_inputs_have_safe_per_file_reasons(monkeypatch):
    for raw,reason in ((b'', 'image_byte_limit'),(b'private malformed file','invalid_or_oversized_raster'),
                       (raster((1,1)),'decorative_pixel'),(raster(format='PPM',mode='RGB'),'unsupported_raster_format')):
        with pytest.raises(visual.ImageUnavailable,match=reason):visual.prepare_image(raw)
    monkeypatch.setattr(visual,'MAX_PIXELS',100)
    with pytest.raises(visual.ImageUnavailable,match='pixel_limit'):visual.prepare_image(raster())
    monkeypatch.setattr(visual,'MAX_BYTES',20)
    with pytest.raises(visual.ImageUnavailable,match='byte_limit'):visual.prepare_image(raster())


def test_decompression_warning_is_rejected_without_exposing_source_bytes():
    with patch.object(visual.Image,'open',side_effect=visual.Image.DecompressionBombWarning('private pixels')):
        with pytest.raises(visual.ImageUnavailable,match='invalid_or_oversized_raster') as error:
            visual.prepare_image(b'not printed')
    assert 'private' not in str(error.value)


def test_transparent_images_are_skipped_and_white_transparent_logos_keep_contrast():
    blank=io.BytesIO();Image.new('RGBA',(32,32),(0,0,0,0)).save(blank,format='PNG')
    with pytest.raises(visual.ImageUnavailable,match='transparent_raster'):visual.prepare_image(blank.getvalue())
    logo=Image.new('RGBA',(32,32),(0,0,0,0))
    for x in range(8,24):
        for y in range(8,24):logo.putpixel((x,y),(255,255,255,255))
    output=io.BytesIO();logo.save(output,format='PNG')
    result=visual.prepare_image(output.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(result.data_url.split(',',1)[1]))) as decoded:
        assert decoded.getpixel((0,0))==(0,0,0) and decoded.getpixel((16,16))==(255,255,255)


@pytest.mark.parametrize('bad',[None,{}, {'model':'other'}, {'model':gemma.MODEL,'data':[None]},
    {'model':gemma.MODEL,'data':[{'index':True,'embedding':[1.]*768}]},
    {'model':gemma.MODEL,'data':[{'index':0,'embedding':[float('nan')]*768}]},
    {'model':gemma.MODEL,'data':[{'index':0,'embedding':[0.]*768}]}])
def test_incomplete_or_wrong_space_responses_never_become_vectors(bad):
    with pytest.raises(ValueError):visual.response_vector(bad)
