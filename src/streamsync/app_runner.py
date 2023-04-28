import multiprocessing
import multiprocessing.synchronize
import multiprocessing.connection
import threading
import asyncio
import concurrent.futures
import importlib.util
import inspect
import os
import sys
import logging
from types import ModuleType
import json
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError
from streamsync.core import StreamsyncSession
from streamsync.ss_types import (AppProcessServerRequest, AppProcessServerRequestPacket, AppProcessServerResponse, AppProcessServerResponsePacket, ComponentUpdateRequest, ComponentUpdateRequestPayload,
                                 EventRequest, EventResponsePayload, InitSessionRequest, InitSessionRequestPayload, InitSessionResponse, InitSessionResponsePayload, StreamsyncEvent)
import watchdog.observers
import watchdog.events
from streamsync import VERSION


class MessageHandlingException(Exception):
    pass


class SessionPruner(threading.Thread):

    """
    Prunes sessions in intervals, without interfering with the AppProcess server thread.  
    """

    PRUNE_SESSIONS_INTERVAL_SECONDS = 60

    def __init__(self,
                 is_session_pruner_terminated: threading.Event):
        super().__init__(name="SessionPrunerThread")
        self.is_session_pruner_terminated = is_session_pruner_terminated

    def run(self) -> None:
        import streamsync

        while True:
            self.is_session_pruner_terminated.wait(
                timeout=SessionPruner.PRUNE_SESSIONS_INTERVAL_SECONDS)
            if self.is_session_pruner_terminated.is_set():
                return
            streamsync.session_manager.prune_sessions()


class AppProcess(multiprocessing.Process):

    """
    Streamsync runs the user's app code using an isolated process, based on this class.
    The main process is able to communicate with the user app process via app messages (e.g. event, componentUpdate).
    """

    def __init__(self,
                 client_conn: multiprocessing.connection.Connection,
                 server_conn: multiprocessing.connection.Connection,
                 app_path: str,
                 mode: str,
                 run_code: str,
                 components: Dict,
                 is_app_process_server_ready: multiprocessing.synchronize.Event):
        super().__init__(name="AppProcess")
        self.client_conn = client_conn
        self.server_conn = server_conn
        self.app_path = app_path
        self.mode = mode
        self.run_code = run_code
        self.components = components
        self.is_app_process_server_ready = is_app_process_server_ready

    def _load_module(self) -> ModuleType:
        """
        Loads the entry point for the user code in module streamsyncuserapp.
        """

        module_name = "streamsyncuserapp"
        spec = importlib.util.spec_from_loader(module_name, loader=None)
        if spec is None:
            raise ModuleNotFoundError("Couldn't load app module spec.")
        module: ModuleType = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        globals()[module_name] = module
        return module

    def _get_user_functions(self) -> List[str]:
        """
        Returns functions exposed in the user code module, which are potential event handlers.
        """

        streamsyncuserapp = sys.modules.get("streamsyncuserapp")
        if streamsyncuserapp is None:
            raise ValueError("Couldn't find app module (streamsyncuserapp).")
        all_user_functions = map(lambda x: x[0], inspect.getmembers(
            streamsyncuserapp, inspect.isfunction))
        exposed_user_functions = list(
            filter(lambda x: not x.startswith("_"), all_user_functions))
        return exposed_user_functions

    def _handle_session_init(self, payload: InitSessionRequestPayload) -> InitSessionResponsePayload:
        """
        Handles session initialisation and provides a starter pack.
        """

        import streamsync
        import traceback as tb

        session = streamsync.session_manager.get_new_session(
            payload.cookies, payload.headers, payload.proposedSessionId)
        if session is None:
            raise MessageHandlingException("Session rejected.")

        user_state = {}
        try:
            user_state = session.session_state.user_state.to_dict()
        except BaseException:
            session.session_state.add_log_entry(
                "error", "Serialisation error", tb.format_exc())

        res_payload = InitSessionResponsePayload(
            userState=user_state,
            sessionId=session.session_id,
            mail=session.session_state.mail,
            components=streamsync.component_manager.to_dict(),
            userFunctions=self._get_user_functions()
        )

        session.session_state.clear_mail()

        return res_payload

    def _handle_event(self, session: StreamsyncSession, event: StreamsyncEvent) -> EventResponsePayload:
        import traceback as tb

        result = session.event_handler.handle(event)

        mutations = {}

        try:
            mutations = session.session_state.user_state.get_mutations_as_dict()
        except BaseException:
            session.session_state.add_log_entry("error",
                                                "Serialisation Error",
                                                f"An exception was raised during serialisation.",
                                                tb.format_exc())

        mail = session.session_state.mail

        res_payload = EventResponsePayload(
            result=result,
            mutations=mutations,
            mail=mail
        )

        session.session_state.clear_mail()

        return res_payload

    def _handle_component_update(self, payload: ComponentUpdateRequestPayload) -> None:
        import streamsync
        streamsync.component_manager.ingest(payload.components)

    def _handle_message(self, session_id: str, request: AppProcessServerRequest) -> AppProcessServerResponse:
        """
        Handles messages from the main process to the app's isolated process.
        """

        import streamsync

        session = None
        type = request.type

        if type == "sessionInit":
            si_req_payload = InitSessionRequestPayload.parse_obj(
                request.payload)
            return AppProcessServerResponse(
                status="ok",
                status_message=None,
                payload=self._handle_session_init(si_req_payload)
            )

        session = streamsync.session_manager.get_session(session_id)
        if not session:
            raise MessageHandlingException("Session not found.")
        session.update_last_active_timestamp()

        if type == "checkSession":
            return AppProcessServerResponse(
                status="ok",
                status_message=None,
                payload=None
            )

        if type == "event":
            ev_req_payload = StreamsyncEvent.parse_obj(request.payload)
            return AppProcessServerResponse(
                status="ok",
                status_message=None,
                payload=self._handle_event(session, ev_req_payload)
            )

        if self.mode == "edit" and type == "componentUpdate":
            cu_req_payload = ComponentUpdateRequestPayload.parse_obj(
                request.payload)
            self._handle_component_update(cu_req_payload)
            return AppProcessServerResponse(
                status="ok",
                status_message=None,
                payload=None
            )

        raise MessageHandlingException("Invalid event.")

    def _execute_user_code(self) -> None:
        """
        Executes the user code and captures standard output.
        """

        import streamsync
        from contextlib import redirect_stdout
        import io

        streamsyncuserapp = sys.modules.get("streamsyncuserapp")
        if streamsyncuserapp is None:
            raise ValueError("Couldn't find app module (streamsyncuserapp).")

        with redirect_stdout(io.StringIO()) as f:
            exec(self.run_code, streamsyncuserapp.__dict__)
        captured_stdout = f.getvalue()

        if captured_stdout:
            streamsync.initial_state.add_log_entry(
                "info", "Stdout message during initialisation", captured_stdout)

    def _apply_configuration(self) -> None:
        import streamsync

        streamsync.Config.mode = self.mode

        if self.mode == "edit":
            streamsync.Config.is_mail_enabled_for_log = True
        elif self.mode == "run":
            streamsync.Config.is_mail_enabled_for_log = False

    def _main(self) -> None:
        self._apply_configuration()
        import os
        os.chdir(self.app_path)
        self._load_module()
        # Allows for relative imports from the app's path
        sys.path.append(self.app_path)

        import streamsync
        import traceback as tb

        try:
            self._execute_user_code()
        except BaseException:
            # Initialisation errors will be sent to all sessions via mail during session initialisation

            streamsync.initial_state.add_log_entry(
                "error", "Code Error", "Couldn't execute code. An exception was raised.", tb.format_exc())

        try:
            streamsync.component_manager.ingest(self.components)
        except BaseException:
            streamsync.initial_state.add_log_entry(
                "error", "UI Components Error", "Couldn't load components. An exception was raised.", tb.format_exc())

        self._run_app_process_server()

    def _handle_message_and_get_packet(self, message_id: int, session_id: str, request: AppProcessServerRequest) -> AppProcessServerResponsePacket:
        response = None
        try:
            response = self._handle_message(session_id, request)
        except (MessageHandlingException, ValidationError) as e:
            response = AppProcessServerResponse(
                status="error",
                status_message=repr(e),
                payload=None
            )

        packet: AppProcessServerResponsePacket = (
            message_id, session_id, response)
        return packet

    def _send_packet(self, packet_future: concurrent.futures.Future) -> None:
        result = packet_future.result()

        with self.server_conn_lock:
            self.server_conn.send(result)

    def _run_app_process_server(self) -> None:
        import signal

        is_app_process_server_terminated = threading.Event()
        session_pruner = SessionPruner(
            is_app_process_server_terminated)
        session_pruner.start()

        def terminate_server():
            with self.server_conn_lock:
                self.server_conn.send(None)
                is_app_process_server_terminated.set()
                session_pruner.join()

        def signal_handler(sig, frame):
            terminate_server()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        with concurrent.futures.ThreadPoolExecutor(100) as thread_pool:
            self.is_app_process_server_ready.set()
            while True:  # Starts app message server
                try:
                    if not self.server_conn.poll(1):
                        continue
                    packet = self.server_conn.recv()
                    if packet is None:  # An empty packet terminates the process
                        # Send empty packet to client for it to close
                        terminate_server()
                        return
                    self._handle_app_process_server_packet(packet, thread_pool)
                except BaseException as e:
                    logging.error(
                        f"Unexpected exception in AppProcess server.\n{repr(e)}")
                    terminate_server()
                    return

    def _handle_app_process_server_packet(self, packet: AppProcessServerRequestPacket, thread_pool) -> None:
        (message_id, session_id, request) = packet
        thread_pool_future = thread_pool.submit(self._handle_message_and_get_packet,
                                                message_id, session_id, request)
        thread_pool_future.add_done_callback(self._send_packet)

    def run(self) -> None:
        self.server_conn_lock = threading.Lock()
        self.client_conn.close()
        self._main()


class FileEventHandler(watchdog.events.PatternMatchingEventHandler):

    """
    Watches for changes in files and triggers code reloads.
    """

    def __init__(self, update_callback: Callable):
        self.update_callback = update_callback
        super().__init__(patterns=["*.py"], ignore_patterns=[
            ".*"], ignore_directories=False, case_sensitive=False)

    def on_any_event(self, event) -> None:
        if event.event_type not in ("modified", "deleted", "created"):
            return
        self.update_callback()


class ThreadSafeAsyncEvent(asyncio.Event):

    """ Asyncio event adapted to be thread-safe."""

    def __init__(self):
        super().__init__()
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

    def set(self) -> None:
        self._loop.call_soon_threadsafe(super().set)


class AppProcessListener(threading.Thread):

    """
    Listens to messages from the AppProcess server.
    Notifies receipt via events in response_events and makes the responses available in response_packets.  
    """

    def __init__(self,
                 client_conn: multiprocessing.connection.Connection,
                 is_app_process_server_ready: multiprocessing.synchronize.Event,
                 response_packets: Dict,
                 response_events: Dict):
        super().__init__(name="AppProcessListenerThread")
        self.client_conn = client_conn
        self.is_app_process_server_ready = is_app_process_server_ready
        self.response_packets = response_packets
        self.response_events = response_events

    def run(self) -> None:
        self.is_app_process_server_ready.wait()
        while True:
            if not self.client_conn.poll(1):
                continue
            try:
                packet = self.client_conn.recv()
            except OSError:
                logging.error("Connection to AppProcess closed.")
                return
            if packet is None:
                return
            message_id = packet[0]
            self.response_packets[message_id] = packet
            response_event = self.response_events.get(message_id)
            if response_event:
                response_event.set()
            else:
                raise ValueError(
                    f"No response event found for message {message_id}.")


class AppRunner:

    """
    Starts a given user app in a separate process.
    Manages changes to the app.
    Allows for communication with the app via messages.
    """

    def __init__(self, app_path: str, mode: str):
        self.server_conn: Optional[multiprocessing.connection.Connection] = None
        self.client_conn: Optional[multiprocessing.connection.Connection] = None
        self.app_process: Optional[AppProcess] = None
        self.saved_code: Optional[str] = None
        self.run_code: Optional[str] = None
        self.components: Optional[Dict] = None
        self.is_app_process_server_ready = multiprocessing.Event()
        self.app_process_listener: Optional[AppProcessListener] = None
        self.run_code_version: int = 0
        self.observer: Any = None
        self.app_path: str = app_path
        self.response_events: Dict[int, ThreadSafeAsyncEvent] = {}
        self.response_packets: Dict[int, AppProcessServerResponsePacket] = {}
        self.message_counter = 0

        if mode not in ("edit", "run"):
            raise ValueError("Invalid mode.")

        self.mode = mode

    def _set_observer(self):
        self.observer = watchdog.observers.Observer()
        self.observer.schedule(
            FileEventHandler(self.reload_code_from_saved), path=self.app_path, recursive=True)
        self.observer.start()

    def load(self) -> None:
        self.saved_code = self._load_persisted_script()
        self.run_code = self.saved_code
        self.components = self._load_persisted_components()

        if self.mode == "edit":
            self._set_observer()

        self._start_app_process()

    def get_run_code_version(self) -> int:
        return self.run_code_version

    async def dispatch_message(self, session_id: Optional[str], request: AppProcessServerRequest) -> AppProcessServerResponse:

        """
        Sends a message to the AppProcess server, waits for the listener to obtain a response and returns it.
        """

        message_id = self.message_counter
        self.message_counter += 1
        is_response_ready = ThreadSafeAsyncEvent()
        self.response_events[message_id] = is_response_ready
        packet: AppProcessServerRequestPacket = (
            message_id, session_id, request)

        if self.client_conn is None:
            raise ValueError(
                "Cannot dispatch message. No connection to AppProcess server is set.")
        self.client_conn.send(packet)

        await is_response_ready.wait()  # Set by the listener thread

        response_packet = self.response_packets.get(message_id)
        if response_packet is None:
            raise ValueError(
                f"Empty packet received in response to message { message_id }.")
        response_message_id, response_session_id, response = response_packet
        del self.response_packets[message_id]
        del self.response_events[message_id]
        if (session_id != response_session_id):
            raise PermissionError("Session mismatch.")
        if (message_id != response_message_id):
            raise PermissionError("Message mismatch.")

        return response

    def _load_persisted_script(self) -> str:
        try:
            with open(os.path.join(self.app_path, "main.py"), "r") as f:
                return f.read()
        except FileNotFoundError:
            logging.error(
                "Couldn't find main.py in the path provided: %s.", self.app_path)
            sys.exit(0)

    def _load_persisted_components(self) -> Dict:
        file_payload: Dict = {}
        try:
            with open(os.path.join(self.app_path, "ui.json"), "r") as f:
                parsed_file = json.load(f)
                if not isinstance(parsed_file, dict):
                    raise ValueError("No dictionary found in components file.")
                file_payload = parsed_file
        except FileNotFoundError:
            logging.error(
                "Couldn't find ui.json in the path provided: %s.", self.app_path)
            sys.exit(0)
        components = file_payload.get("components")
        if components is None:
            raise ValueError("Components not found in file.")
        return components

    async def check_session(self, session_id: str) -> bool:
        response = await self.dispatch_message(session_id, AppProcessServerRequest(
            type="checkSession",
            payload=None
        ))
        is_ok: bool = response.status == "ok"
        return is_ok

    async def init_session(self, payload: InitSessionRequestPayload) -> AppProcessServerResponse:
        return await self.dispatch_message(None, InitSessionRequest(
            type="sessionInit",
            payload=payload
        ))

    async def update_components(self, session_id: str, payload: ComponentUpdateRequestPayload) -> AppProcessServerResponse:
        if self.mode != "edit":
            raise PermissionError(
                "Cannot update components in non-update mode.")
        self.components = payload.components
        file_contents = {
            "metadata": {
                "streamsync_version": VERSION
            },
            "components": payload.components
        }
        with open(os.path.join(self.app_path, "ui.json"), "w") as f:
            json.dump(file_contents, f, indent=4)
        return await self.dispatch_message(session_id, ComponentUpdateRequest(
            type="componentUpdate",
            payload=payload
        ))

    async def handle_event(self, session_id: str, event: StreamsyncEvent) -> AppProcessServerResponse:
        return await self.dispatch_message(session_id, EventRequest(
            type="event",
            payload=event
        ))

    def save_code(self, session_id: str, saved_code: str) -> None:
        if self.mode != "edit":
            raise PermissionError("Cannot save code in non-edit mode.")

        with open(os.path.join(self.app_path, "main.py"), "w") as f:
            f.write(saved_code)
        self.saved_code = saved_code

    def _clean_process(self) -> None:
        # Terminate the AppProcess server by sending an empty message
        # The empty message will bounce an empty message and terminate the client too
        if self.client_conn is not None:
            self.client_conn.send(None)
        self.is_app_process_server_ready.clear()
        if self.app_process is not None:
            self.app_process.join()
            self.app_process.close()
        if self.app_process_listener is not None:
            self.app_process_listener.join()
        if self.client_conn is not None:
            self.client_conn.close()
        if self.server_conn is not None:
            self.server_conn.close()
        self.response_events = {}
        self.response_packets = {}
        self.app_process = None
        self.app_process_listener = None
        self.client_conn = None
        self.server_conn = None

    def shut_down(self) -> None:
        logging.warning("AppRunner shutting down...")
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()
        self._clean_process()

    def _start_app_process(self) -> None:
        if self.run_code is None:
            raise ValueError("Cannot start app process. Code hasn't been set.")
        if self.components is None:
            raise ValueError(
                "Cannot start app process. Components haven't been set.")
        self.is_app_process_server_ready.clear()
        self.client_conn, self.server_conn = multiprocessing.Pipe(duplex=True)
        self.app_process = AppProcess(
            client_conn=self.client_conn,
            server_conn=self.server_conn,
            app_path=self.app_path,
            mode=self.mode,
            run_code=self.run_code,
            components=self.components,
            is_app_process_server_ready=self.is_app_process_server_ready)
        self.app_process.start()
        self.app_process_listener = AppProcessListener(
            self.client_conn,
            self.is_app_process_server_ready,
            self.response_packets,
            self.response_events)
        self.app_process_listener.start()
        self.is_app_process_server_ready.wait()

    def reload_code_from_saved(self) -> None:
        if not self.is_app_process_server_ready.is_set():
            return
        self.saved_code = self._load_persisted_script()
        self.update_code(None, self.saved_code)

    def update_code(self, session_id: Optional[str], run_code: str) -> None:
        if self.mode != "edit":
            raise PermissionError("Cannot update code in non-edit mode.")
        if not self.is_app_process_server_ready.is_set():
            return
        self.run_code = run_code
        self._clean_process()
        self._start_app_process()

        self.is_app_process_server_ready.wait()
        self.run_code_version += 1