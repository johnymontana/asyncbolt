import asyncio
import collections
import logging

from asyncbolt import protocol


logger = logging.getLogger(__name__)
log_debug = logger.debug
log_info = logger.info
log_warning = logger.warning
log_error = logger.error


async def create_server(loop, protocol_class, host='localhost', port=8888, **kwargs):
    server = Server(loop, protocol_class, host=host, port=port, **kwargs)
    await server.start_serving()
    return server


class ServerSession(protocol.BoltServerProtocol):
    """asyncio based implementation of a Bolt server session"""

    def __init__(self, loop, **kwargs):
        super().__init__(loop)
        self.server = kwargs.get('server')  # I want to be managed!
        self.queue = asyncio.Queue()
        self.task_queue_handler = self.loop.create_task(self._run_task_queue())
        self.waiters = collections.deque()
        self.waiters_append = self.waiters.append
        self.waiters_popleft = self.waiters.popleft
        self.close_handler = None

    def connection_made(self, transport):
        if self.server:
            self.server.add_connection(self)
        super().connection_made(transport)

    def connection_lost(self, exc):
        if self.server:
            self.server.remove_connection(self)
        self.close()

    def close(self):
        self.task_queue_handler.cancel()
        self.transport = None
        self.state = protocol.ServerProtocolState.PROTOCOL_CLOSING

    async def wait_closed(self):
        await self.task_queue_handler
        self.task_queue_handler = None
        self.state = protocol.ServerProtocolState.PROTOCOL_CLOSED

    def restart_task_queue(self):
        if self.task_queue_handler:
            self.task_queue_handler.cancel()
        self.task_queue_handler = self.loop.create_task(self._run_task_queue())

    async def _run_task_queue(self):
        try:
            task, future = await self.queue.get()
            fields = await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.state = protocol.ServerProtocolState.PROTOCOL_FAILED
            self.failure({})
            try:
                # TODO should have a timeout here
                # This is a bit weird
                await future
                self.ignored({})
            except:
                pass
            self.flush()
        else:
            self.success({})
            self.record([fields])
            self.success({})
            await future  # Pull All is called, flush queue...
            self.flush()
            self.task_queue_handler = self.loop.create_task(self._run_task_queue())
            log_debug("Packed fields '{}'".format(fields))

    def on_ack_failure(self):
        self.restart_task_queue()

    def on_discard_all(self):
        self.restart_task_queue()

    def on_pull_all(self):
        waiter = self.waiters_popleft()
        waiter.set_result(True)

    def on_reset(self):
        # Check behaviour
        while not self.queue.empty():
            self.queue.get_nowait()
            self.ignored({})
        self.restart_task_queue()

    def on_run(self, data):
        future = asyncio.Future(loop=self.loop)
        self.waiters_append(future)
        self.queue.put_nowait((self.run(data), future))

    async def run(self, data):
        """Inheriting server protocol must implement this method."""
        raise NotImplementedError("""Server received run message {}
                                     Inheriting classes must implement `run`""".format(data))


class Server:
    """
    Server class similar to asyncio.Server. Manage protocol instances and perform graceful shutdown.
    """
    def __init__(self, loop, protocol_class, host='localhost', port=8888, **kwargs):
        self._loop = loop
        self._protocol_class = protocol_class
        self._host = host
        self._port = port
        self._kwargs = kwargs
        self._connections = set()
        self._server = None
        self._old_conns = asyncio.Queue()
        self._cleanup_task = self._loop.create_task(self._do_cleanup())

    async def start_serving(self):
        self._server = await self._loop.create_server(
            lambda: self._protocol_class(self._loop, server=self, **self._kwargs), self._host, self._port)

    @property
    def sockets(self):
        if self._server:
            return self._server.sockets

    async def _do_cleanup(self):
        try:
            old_con = await self._old_conns.get()
            await old_con.wait_closed()
            log_debug('Closed server connection {}'.format(old_con))
        except asyncio.CancelledError:
            pass
        else:
            self._cleanup_task = self._loop.create_task(self._do_cleanup())

    def add_connection(self, connection):
        log_debug('Adding connection {}'.format(connection))
        self._connections.add(connection)

    def remove_connection(self, connection):
        log_debug('Removing connection {}'.format(connection))
        if connection in self._connections:
            self._connections.remove(connection)
            self._old_conns.put_nowait(connection)

    def close(self):
        self._cleanup_task.cancel()

    async def wait_closed(self):
        await self._cleanup_task
        tasks = []
        while not self._old_conns.empty():
            old_con = self._old_conns.get_nowait()
            log_debug('Closing server connection {}'.format(old_con))
            tasks.append(self._loop.create_task(old_con.wait_closed()))
        for con in self._connections:
            log_debug('Closing server connection {}'.format(con))
            con.close()
            tasks.append(self._loop.create_task(con.wait_closed()))
        await asyncio.gather(*tasks)
        log_debug('All server connections closed')
        self._server.close()
        await self._server.wait_closed()
