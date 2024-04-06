from asyncio import Future, IncompleteReadError, LimitOverrunError, StreamReader, StreamReaderProtocol, StreamWriter, Task
import asyncio
from dataclasses import dataclass
import json
from os import system
from typing import Any, Generator, List, MutableMapping, Optional, Sequence


class JsonStreamReader(StreamReader):
    _exception: Optional[Exception]
    _buffer: bytearray
    _eof: bool
    _limit: int

    async def readjson(self) -> Any:
        """Read data from the stream until a complete json object is assembled.

        If an EOF occurs and tha complete object is still not found,
        an IncompleteReadError exception will be raised, and the internal
        buffer will be reset.

        If the data cannot be read because of over limit, a
        LimitOverrunError exception  will be raised, and the data
        will be left in the internal buffer, so it can be read again.
        """
        if self._exception is not None:
            raise self._exception

        obj = None

        while True:
            # Skip initial whitespaces
            while len(self._buffer) != 0 and self._buffer[0:1].isspace():
                del self._buffer[0]

            if len(self._buffer) > 0:
                first = self._buffer[0:1]
                assert first in (b'{', b'['), f"Stream contains non-json-start: {first} - {bytes(self._buffer)}"

                try:
                    obj = json.loads(self._buffer)
                    sep = len(self._buffer)-1
                    break
                except json.JSONDecodeError as e:
                    if e.msg == 'Extra data':
                        sep = e.pos
                        break

            # Complete message (with full separator) may be present in buffer
            # even when EOF flag is set. This may happen when the last chunk
            # adds data which makes separator be found. That's why we check for
            # EOF *ater* inspecting the buffer.
            if self._eof:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                raise IncompleteReadError(chunk, None)

            # _wait_for_data() will resume reading if stream was paused.
            await self._wait_for_data('readjson') # type: ignore

        if sep > self._limit:
            raise LimitOverrunError(
                'Object found, but chunk is longer than limit', sep)

        chunk = self._buffer[:sep]
        del self._buffer[:sep]
        if obj is None:
            obj = json.loads(chunk)
        self._maybe_resume_transport() # type: ignore
        return obj


@dataclass
class SpuEntry:
    index : int
    language: str
    description: str

SpuList = Sequence[SpuEntry]

@dataclass
class Request:
    request_id : int
    request_code : str
    request_reply : Any
    completed: Future['Request']

    def bytes(self) -> bytes:
        message = f"{self.request_id}:{self.request_code}"
        msg_length = len(message)
        full_message = f"{msg_length}:{message}"
        return full_message.encode('utf8')


class VLCApi:
    reader : JsonStreamReader
    writer : StreamWriter

    running: bool

    reader_task : Optional[Task[None]]

    request_count: int

    current_requests: MutableMapping[int, Request]

    read_exception: Optional[Exception]

    shutdown: Future[None]

    def __init__(self):
        self.request_count = 0
        self.running = False
        self.current_requests = dict()
        self.read_exception = None
    
    async def _open_connection(self, host: str, port: int, limit: int = 65536):
        """asyncio.open_connection says to copy-paste-modify to change reader classes"""
        loop = asyncio.get_running_loop()
        reader = JsonStreamReader(limit=limit, loop=loop)
        protocol = StreamReaderProtocol(reader, loop=loop)
        transport, _ = await loop.create_connection(
            lambda: protocol, host, port)
        writer = StreamWriter(transport, protocol, reader, loop)
        return reader, writer
    
    async def connect(self, host: str, port: int, wait_until_started: bool=False):
        assert self.running == False
        async def __connect():
            self.reader, self.writer = await self._open_connection(host, port)

        try:
            try:
                await __connect()
            except ConnectionRefusedError:
                print("vlc not available, waiting for signal that it has started")
                system("waitfor VlcStarted")
                print("signal received that vlc has started")
                await __connect()

            self.shutdown = asyncio.get_running_loop().create_future()
            self.running = True            
            self.reader_task = asyncio.create_task(self.read())
            assert await self.test_connection()
        except:
            self.running = False
            if not self.shutdown.cancelled():
                self.shutdown.cancel()
            raise
    
    async def read(self) -> Any:
        try:
            while self.running:
                reply = await self.reader.readjson()
                reply_id: Optional[int] = reply.get('reply_id', None)

                if reply_id is None:
                    # unsolicited communication
                    #  callbacks etc
                    continue

                request = self.current_requests.get(reply_id, None)
                if not request:
                    # The request was dropped before it was answered?
                    continue

                request.request_reply = reply
                if not request.completed.cancelled():
                    request.completed.set_result(request)

        except (RuntimeError, IncompleteReadError) as e:
            self.read_exception = e
            for request in self.current_requests.values():
                if not request.completed.cancelled():
                    request.completed.cancel()
            raise

        finally:
            self.reader_task = None
            if not self.shutdown.cancelled():
                self.shutdown.cancel()


    
    async def test_connection(self) -> bool:
        res = await self.execute("return 2+2")
        return res['result'] == 4
    
    def issue_request(self, lua: str) -> Future[Request]:
        if self.read_exception:
            raise self.read_exception
        
        req_id = self.request_count
        self.request_count += 1

        request = Request(request_id=req_id, request_code=lua, request_reply=None, completed=asyncio.get_running_loop().create_future())
        self.current_requests[req_id] = request

        self.writer.write(request.bytes())

        def cleanup(future: Future[Any]):
            del self.current_requests[req_id]

        request.completed.add_done_callback(cleanup)

        return request.completed

    
    async def execute(self, lua: str) -> Any:

        reply = await self.issue_request(lua=lua)

        obj = reply.request_reply

        if obj['timeout']:
            raise TimeoutError(f"Failed to execute '{lua}' within time constraints by vlc host")
        error: Optional[str] = obj.get('error', None)
        if error is not None:
            raise RuntimeError(f"Failed to execute '{lua}': {error}")
        return obj

    
    async def get_spu_entries(self) -> Generator[SpuEntry, None, None]:
        response = await self.execute('return {vlc.var.get_list(vlc.object.input(), "spu-es")}')
        indices: List[int]
        descriptions: List[str]
        indices, descriptions = response['result']
        def work():
            for index, text in zip(indices, descriptions):
                a,b = (text.split(" - ", maxsplit=1) + ['none'])[0:2]
                language = b.replace('[', '').replace(']', '')
                description = a
                yield SpuEntry(index=index, language=language, description=description)
        return work()

    async def get_spu_list(self) -> SpuList:
        return list(await self.get_spu_entries())
    
    async def select_spu(self, spu: SpuEntry):
        await self.execute(f'vlc.var.set(vlc.object.input(), "spu-es", {spu.index})')


async def run():
    while True:

        api = VLCApi()
        await api.connect("localhost", 9998, wait_until_started=True)
        spu_list = await api.get_spu_list()
        candidates = [spu for spu in spu_list if "english" in spu.language.lower()]
        if len(candidates) == 1:
            await api.select_spu(candidates[0])
        candidates = [spu for spu in candidates if "full" in spu.description.lower()]
        if len(candidates) == 1:
            await api.select_spu(candidates[0])
        
        await asyncio.wait((api.shutdown,), timeout=60, return_when=asyncio.FIRST_COMPLETED)




def main():
    asyncio.run(run())



    

if __name__ == "__main__":
    main()
