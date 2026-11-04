"""Bounded raster inputs and strict same-space visual embedding responses."""
import base64
from dataclasses import dataclass
import hashlib
import io
import math
import warnings

from PIL import Image, ImageOps

from . import gemma

VERSION = "embeddinggemma2-visual-v1"
MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000


class ImageUnavailable(ValueError):
    """A predefined, safe per-file reason; never includes image bytes or URLs."""


@dataclass(frozen=True)
class ImageInput:
    source_sha256: str
    width: int
    height: int
    data_url: str
    frame: int = 0

    def content(self):
        return {"content": [{"type": "image_url", "image_url": {"url": self.data_url}}]}

    def provenance(self):
        return {"version": VERSION, "source_sha256": self.source_sha256,
                "width": self.width, "height": self.height, "frame": self.frame,
                "processing": "exif-rgb-contrast-alpha-fit1600-first-visible8"}


def prepare_image(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ImageUnavailable("image_byte_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                width,height=source.size
                if width*height > MAX_PIXELS or max(width,height)>32768:
                    raise ImageUnavailable("image_pixel_limit")
                if min(width,height)<=2:
                    raise ImageUnavailable("decorative_pixel")
                if source.format not in ("PNG","JPEG","GIF","WEBP","BMP","TIFF"):
                    raise ImageUnavailable("unsupported_raster_format")
                image=None;frame=0
                for frame in range(8 if source.format=='GIF' else 1):
                    try:source.seek(frame)
                    except EOFError:break
                    candidate=ImageOps.exif_transpose(source).convert("RGBA")
                    if candidate.getchannel('A').getextrema()!=(0,0):
                        image=candidate;break
                if image is None:raise ImageUnavailable('transparent_raster')
                image.thumbnail((1600,1600),Image.Resampling.LANCZOS)
                small=image.copy();small.thumbnail((64,64))
                pixels=list(small.getdata())
                weight=sum(a for _,_,_,a in pixels)
                light=sum((.2126*r+.7152*g+.0722*b)*a for r,g,b,a in pixels)/weight if weight else 0
                background=Image.new("RGBA",image.size,"black" if light>200 else "white")
                background.alpha_composite(image)
                output=io.BytesIO()
                background.convert("RGB").save(output,format="PNG",compress_level=1)
    except ImageUnavailable:
        raise
    except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ImageUnavailable("invalid_or_oversized_raster") from None
    data="data:image/png;base64,"+base64.b64encode(output.getvalue()).decode("ascii")
    return ImageInput(hashlib.sha256(raw).hexdigest(),width,height,data,frame)


def response_vector(payload):
    if not isinstance(payload,dict) or payload.get("model")!=gemma.MODEL:
        raise ValueError("Visual embedding response model mismatch")
    rows=payload.get("data")
    if (not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict)
            or type(rows[0].get("index")) is not int or rows[0]["index"]!=0):
        raise ValueError("Visual embedding response index mismatch")
    vector=gemma.vector(rows[0].get("embedding",[]))
    norm=math.sqrt(sum(value*value for value in vector))
    return [value/norm for value in vector]
