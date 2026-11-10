"""Tamano de salida (?ow=300): para TVs (Nuvio Legacy) que si no bajan el poster de 500x750
y lo reducen ellas mismas a ~260-300 px. Middleware ASGI: quita `ow` de la query ANTES de que
la app la vea (misma clave de cache, mismo render) y reduce solo la imagen que se entrega."""
import io
from urllib.parse import parse_qsl, urlencode

from PIL import Image

MIN_W, MAX_W = 64, 2000


class OutputSizeMiddleware:
    def __init__(self, app, quality: int = 88):
        self.app = app
        self.quality = quality

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        qs = scope.get("query_string", b"").decode("latin-1")
        if "ow=" not in qs:
            return await self.app(scope, receive, send)
        pairs = parse_qsl(qs, keep_blank_values=True)
        ow = None
        for k, v in pairs:
            if k == "ow":
                try:
                    ow = int(v)
                except ValueError:
                    ow = None
        rest = [(k, v) for k, v in pairs if k != "ow"]
        scope = dict(scope, query_string=urlencode(rest).encode("latin-1"))
        if not ow or not (MIN_W <= ow <= MAX_W):
            return await self.app(scope, receive, send)

        start = {}
        chunks = []

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    await self._finish(start, b"".join(chunks), ow, send)

        await self.app(scope, receive, capture)

    async def _finish(self, start, body, ow, send):
        headers = list(start.get("headers", []))
        ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
        if start.get("status") == 200 and ctype.startswith(b"image/") and body:
            try:
                im = Image.open(io.BytesIO(body))
                if im.width > ow:
                    h = max(1, round(im.height * ow / im.width))
                    im = im.convert("RGB").resize((ow, h), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=self.quality, optimize=True)
                    body = buf.getvalue()
                    headers = [(k, v) for k, v in headers
                               if k.lower() not in (b"content-length", b"content-type", b"etag")]
                    headers += [(b"content-type", b"image/jpeg")]
            except Exception:
                pass
        headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": start.get("status", 200), "headers": headers})
        await send({"type": "http.response.body", "body": body})
