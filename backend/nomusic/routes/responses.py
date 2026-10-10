"""Media responses that own a lease through completion or disconnect."""

from fastapi.responses import FileResponse, StreamingResponse


class LeasedFileResponse(FileResponse):
    def __init__(self, path: str, lease, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()


class LeasedStreamingResponse(StreamingResponse):
    def __init__(self, content, lease, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()
