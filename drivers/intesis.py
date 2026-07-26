"""Thread-safe Intesis Cloud driver built on pyIntesisHome 2.0.3.

This module has no Flask dependency.  A dedicated asyncio event loop keeps the
pyIntesisHome controller, TCP connection, receive task and aiohttp session alive
for the lifetime of the driver.  Callers use the synchronous public API below;
they never need to import or interact with pyIntesisHome directly.
"""

import asyncio
import copy
import logging
import threading
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, Optional


DEFAULT_INTESIS_CONFIG = {
    "enabled": False,
    "username": "",
    "password": "",
    "device_type": "IntesisHome",
    "poll_seconds": 300,
    "reconnect_seconds": 30,
    "command_timeout_seconds": 15,
}

INTESIS_COMMAND_PUSH_WAIT_SECONDS = 2.0


def _number_setting(value, default, minimum, number_type=int):
    try:
        return max(minimum, number_type(value))
    except (TypeError, ValueError):
        return number_type(default)


class _IntesisLogHandler(logging.Handler):
    """Forward safe INFO-or-higher library messages to the MeM logger."""

    def __init__(self, callback):
        super().__init__(level=logging.INFO)
        self.callback = callback

    def emit(self, record):
        try:
            self.callback(
                f"Intesis library: {record.getMessage()}",
                level=record.levelname,
            )
        except Exception:
            pass


class IntesisDriver:
    """Own and supervise one persistent Intesis Cloud controller."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable] = None,
        controller_factory: Optional[Callable] = None,
    ):
        settings = {**DEFAULT_INTESIS_CONFIG, **(config or {})}
        self.enabled = bool(settings.get("enabled"))
        self.username = str(settings.get("username") or "").strip()
        self.password = str(settings.get("password") or "")
        self.device_type = str(settings.get("device_type") or "IntesisHome").strip()
        self.poll_seconds = _number_setting(
            settings.get("poll_seconds"), 300, 300
        )
        self.reconnect_seconds = _number_setting(
            settings.get("reconnect_seconds"), 30, 5
        )
        self.command_timeout_seconds = _number_setting(
            settings.get("command_timeout_seconds"), 15, 5.0, float
        )
        self.log_callback = log_callback
        self.controller_factory = controller_factory

        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._ready = threading.Event()
        self._thread = None
        self._loop = None
        self._controller = None
        self._background_task = None
        self._async_stop = None
        self._library_log_handler = None
        self._devices = {}
        self._connected = False
        self._ever_connected = False
        self._last_error = None
        self._last_poll = None
        self._last_success = None
        self._failure_count = 0
        self._device_update_revisions = {}

    def connect(self) -> bool:
        """Start the persistent cloud session and automatic maintenance loop."""
        if not self.enabled:
            self._log("Intesis driver disabled.", level="DEBUG")
            return False
        if not self.username or not self.password:
            self._set_error("username and password are required")
            self._log(
                "ERRORE Intesis connection: username and password are required"
            )
            return False

        with self._lifecycle_lock:
            if not self._thread or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="intesis-driver",
                    daemon=True,
                )
                self._thread.start()

        self._ready.wait(self.command_timeout_seconds)
        return self.is_connected

    def disconnect(self) -> bool:
        """Close the cloud connection, aiohttp session and driver event loop."""
        with self._lifecycle_lock:
            loop = self._loop
            thread = self._thread
            if not loop or not thread or not thread.is_alive():
                self._set_connected(False)
                return True

            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                future.result(timeout=self.command_timeout_seconds)
            except Exception as exc:  # the process must remain alive during shutdown
                self._log(f"ERRORE Intesis disconnect: {exc}")
                self._log(
                    f"TRACEBACK Intesis disconnect:\n{traceback.format_exc()}",
                    level="DEBUG",
                )
            finally:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)

        thread.join(timeout=self.command_timeout_seconds)
        stopped = not thread.is_alive()
        if stopped:
            self._log("Intesis connection closed.")
        else:
            self._log(
                "ERRORE Intesis disconnect: driver thread is still stopping "
                "after timeout"
            )
        return stopped

    def poll(self) -> bool:
        """Refresh discovery and status without replacing the persistent session."""
        if not self._ensure_thread():
            return False
        return bool(self._submit(self._poll_once(log_success=True), default=False))

    def get_all_devices(self):
        """Return a detached list containing every discovered normalized device."""
        with self._state_lock:
            return copy.deepcopy(list(self._devices.values()))

    def get_device(self, device_id):
        """Return one detached normalized device, or None when it is unknown."""
        with self._state_lock:
            device = self._devices.get(str(device_id))
            return copy.deepcopy(device) if device is not None else None

    def power_on(self, device_id) -> bool:
        return self._command(device_id, "set_power_on")

    def power_off(self, device_id) -> bool:
        return self._command(device_id, "set_power_off")

    def set_mode(self, device_id, mode) -> bool:
        return self._command(device_id, "set_mode", str(mode).lower())

    def set_temperature(self, device_id, temperature) -> bool:
        try:
            value = round(float(temperature), 1)
        except (TypeError, ValueError):
            self._log(
                f"ERRORE Intesis command set_temperature for {device_id}: "
                "invalid value"
            )
            return False
        return self._command(device_id, "set_temperature", value)

    def set_fan_speed(self, device_id, speed) -> bool:
        return self._command(device_id, "set_fan_speed", str(speed).lower())

    def set_vertical_vane(self, device_id, position) -> bool:
        return self._command(device_id, "set_vertical_vane", str(position).lower())

    def set_horizontal_vane(self, device_id, position) -> bool:
        return self._command(device_id, "set_horizontal_vane", str(position).lower())

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def status(self):
        """Return driver diagnostics without exposing credentials."""
        with self._state_lock:
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "device_count": len(self._devices),
                "last_poll": self._last_poll,
                "last_successful_poll": self._last_success,
                "consecutive_failures": self._failure_count,
                "last_error": self._last_error,
            }

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._bootstrap())
            loop.run_forever()
        except Exception as exc:
            self._set_error(str(exc))
            self._log(f"ERRORE Intesis event loop: {exc}")
            self._log(
                f"TRACEBACK Intesis event loop:\n{traceback.format_exc()}",
                level="DEBUG",
            )
        finally:
            try:
                if not loop.is_closed():
                    loop.run_until_complete(self._shutdown())
            except Exception as exc:
                self._log(f"ERRORE Intesis resource cleanup: {exc}")
                self._log(
                    f"TRACEBACK Intesis resource cleanup:\n{traceback.format_exc()}",
                    level="DEBUG",
                )
            pending = asyncio.all_tasks(loop) if not loop.is_closed() else set()
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            if not loop.is_closed():
                loop.close()
            with self._state_lock:
                self._loop = None
                self._thread = None
                self._controller = None
                self._connected = False

    async def _bootstrap(self):
        self._async_stop = asyncio.Event()
        try:
            await self._ensure_connected()
            await self._sync_devices()
            with self._state_lock:
                self._last_poll = self._timestamp()
                self._last_success = self._last_poll
                self._last_error = None
                self._failure_count = 0
        except Exception as exc:
            self._mark_offline(exc)
            self._log(f"ERRORE Intesis initial connection: {exc}")
            self._log(
                f"TRACEBACK Intesis initial connection:\n{traceback.format_exc()}",
                level="DEBUG",
            )
        finally:
            self._ready.set()
        self._background_task = asyncio.create_task(self._background_loop())

    async def _background_loop(self):
        next_poll = asyncio.get_running_loop().time() + self.poll_seconds
        while not self._async_stop.is_set():
            delay = self.reconnect_seconds
            try:
                await self._ensure_connected()
                current = asyncio.get_running_loop().time()
                if current >= next_poll:
                    poll_succeeded = await self._poll_once(log_success=True)
                    next_poll = current + (
                        self.poll_seconds if poll_succeeded else self.reconnect_seconds
                    )
                delay = min(self.reconnect_seconds, max(1, int(next_poll - current)))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._mark_offline(exc)
                self._log(f"Intesis recovery failed: {exc}")
                self._log(
                    f"TRACEBACK Intesis recovery:\n{traceback.format_exc()}",
                    level="DEBUG",
                )

            try:
                await asyncio.wait_for(self._async_stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _shutdown(self):
        if self._async_stop is not None:
            self._async_stop.set()
        current = asyncio.current_task()
        if (
            self._background_task
            and self._background_task is not current
            and not self._background_task.done()
        ):
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        self._background_task = None

        controller = self._controller
        self._controller = None
        if controller is not None:
            try:
                await controller.stop()
            except Exception as exc:
                self._log(f"ERRORE Intesis controller cleanup: {exc}")
                self._log(
                    f"TRACEBACK Intesis controller cleanup:\n{traceback.format_exc()}",
                    level="DEBUG",
                )
        if self._library_log_handler is not None:
            logging.getLogger("pyintesishome").removeHandler(self._library_log_handler)
            self._library_log_handler = None
        self._set_connected(False)

    async def _create_controller(self):
        if self.controller_factory is None:
            from pyintesishome import IntesisHome

            factory = IntesisHome
        else:
            factory = self.controller_factory
        if self._library_log_handler is None:
            self._library_log_handler = _IntesisLogHandler(self._log)
            logging.getLogger("pyintesishome").addHandler(self._library_log_handler)
        controller = factory(
            self.username,
            self.password,
            device_type=self.device_type,
        )
        controller.add_update_callback(self._on_update)
        self._controller = controller
        return controller

    async def _ensure_connected(self):
        controller = self._controller or await self._create_controller()
        if controller.is_connected:
            self._update_connection_state()
            return controller

        if self._ever_connected:
            if self._failure_count:
                self._log(
                    f"Intesis recovery started after {self._failure_count} "
                    "consecutive failures."
                )
            else:
                self._log("Intesis recovery started.")
        else:
            self._log("Intesis connection started.")
        await controller.connect()
        self._update_connection_state()
        if not controller.is_connected:
            error = getattr(controller, "error_message", None)
            raise ConnectionError(error or "Intesis Cloud connection was not established")
        return controller

    async def _poll_once(self, log_success=False):
        self._log("Intesis polling: poll cycle started", level="DEBUG")
        try:
            controller = await self._ensure_connected()
            await controller.poll_status(sendcallback=False)
            await self._sync_devices()
            with self._state_lock:
                previous_failures = self._failure_count
                self._last_poll = self._timestamp()
                self._last_success = self._last_poll
                self._last_error = None
                self._failure_count = 0
            if previous_failures:
                self._log(
                    f"Intesis polling restored after {previous_failures} failures."
                )
            if log_success:
                self._log_poll_success(self.poll_seconds)
            return True
        except Exception as exc:
            error_traceback = traceback.format_exc()
            self._mark_offline(exc)
            self._log(f"ERRORE Intesis polling: {exc}")
            self._log(
                f"TRACEBACK Intesis polling:\n{error_traceback}",
                level="DEBUG",
            )
            self._log_poll_failure(self.reconnect_seconds)
            return False

    async def _on_update(self, device_id=None):
        try:
            self._update_connection_state()
            await self._sync_devices()
            if device_id is not None:
                device_id = str(device_id)
                self._device_update_revisions[device_id] = (
                    self._device_update_revisions.get(device_id, 0) + 1
                )
        except Exception as exc:
            self._set_error(str(exc))
            self._log(f"ERRORE Intesis push update: {exc}")
            self._log(
                f"TRACEBACK Intesis push update:\n{traceback.format_exc()}",
                level="DEBUG",
            )

    async def _sync_devices(self):
        controller = self._controller
        if controller is None:
            return
        raw_devices = controller.get_devices() or {}
        normalized = {
            str(device_id): self._normalize_device(controller, str(device_id), raw)
            for device_id, raw in list(raw_devices.items())
        }
        with self._state_lock:
            self._devices = normalized

    def _normalize_device(self, controller, device_id, raw):
        raw = raw if isinstance(raw, dict) else {}
        power_state = self._safe_get(controller, "get_power_state", device_id)
        if power_state == "on":
            power = True
        elif power_state == "off":
            power = False
        else:
            power = None

        rssi = self._native_number(self._safe_get(controller, "get_rssi", device_id))
        working_hours = self._native_number(
            self._safe_get(controller, "get_run_hours", device_id)
        )
        supported_modes = self._string_list(
            self._safe_get(controller, "get_mode_list", device_id)
        )
        supported_fan_speeds = self._string_list(
            self._safe_get(controller, "get_fan_speed_list", device_id)
        )
        supported_vertical_vanes = self._string_list(
            self._safe_get(controller, "get_vertical_swing_list", device_id)
        )
        supported_horizontal_vanes = self._string_list(
            self._safe_get(controller, "get_horizontal_swing_list", device_id)
        )
        vertical_vane = self._safe_get(
            controller, "get_vertical_swing", device_id
        )
        horizontal_vane = self._safe_get(
            controller, "get_horizontal_swing", device_id
        )
        error_code = self._native_number(raw.get("error_code"))
        return {
            "id": str(device_id),
            "name": self._safe_get(controller, "get_device_name", device_id)
            or raw.get("name")
            or f"Device {device_id}",
            "power": power,
            "mode": self._safe_get(controller, "get_mode", device_id),
            "room_temperature": self._native_number(
                self._safe_get(controller, "get_temperature", device_id)
            ),
            "target_temperature": self._native_number(
                self._safe_get(controller, "get_setpoint", device_id)
            ),
            "minimum_target_temperature": self._native_number(
                self._safe_get(controller, "get_min_setpoint", device_id)
            ),
            "maximum_target_temperature": self._native_number(
                self._safe_get(controller, "get_max_setpoint", device_id)
            ),
            "fan_speed": self._safe_get(controller, "get_fan_speed", device_id),
            "vertical_vane": vertical_vane,
            "horizontal_vane": horizontal_vane,
            "supported_modes": supported_modes,
            "supported_fan_speeds": supported_fan_speeds,
            "supported_vertical_vanes": supported_vertical_vanes,
            "supported_horizontal_vanes": supported_horizontal_vanes,
            "supports_vertical_vane": bool(
                supported_vertical_vanes or vertical_vane is not None
            ),
            "supports_horizontal_vane": bool(
                supported_horizontal_vanes or horizontal_vane is not None
            ),
            "rssi": rssi,
            "wifi": rssi,
            "working_hours": working_hours,
            "error_code": error_code,
            "error": self._safe_get(controller, "get_error", device_id),
            "model": raw.get("model"),
            "last_update": self._timestamp(),
        }

    def _command(self, device_id, method_name, *args) -> bool:
        if not self._ensure_thread():
            return False
        return bool(
            self._submit(
                self._execute_command(str(device_id), method_name, *args),
                default=False,
            )
        )

    async def _execute_command(self, device_id, method_name, *args):
        try:
            controller = await self._ensure_connected()
            if controller.get_device(device_id) is None:
                await controller.poll_status(sendcallback=False)
            if controller.get_device(device_id) is None:
                raise KeyError(f"unknown Intesis device {device_id}")

            method = getattr(controller, method_name)
            previous_revision = self._device_update_revisions.get(device_id, 0)
            self._log(
                f"Intesis command: {method_name} | device={device_id}"
                + (f" | value={args[0]}" if args else ""),
                level="DEBUG",
            )
            acknowledged = bool(await method(device_id, *args))
            if not acknowledged:
                self._log(
                    f"ERRORE Intesis command {method_name} for {device_id}: "
                    "no acknowledgement"
                )
                return False
            await self._refresh_after_command(
                controller,
                device_id,
                previous_revision,
            )
            self._log(
                f"Intesis command {method_name} completed for {device_id}.",
                level="DEBUG",
            )
            return True
        except Exception as exc:
            self._set_error(str(exc))
            self._update_connection_state()
            self._log(
                f"ERRORE Intesis command {method_name} for {device_id}: {exc}"
            )
            self._log(
                f"TRACEBACK Intesis command {method_name}:\n"
                f"{traceback.format_exc()}",
                level="DEBUG",
            )
            return False

    async def _refresh_after_command(self, controller, device_id, previous_revision):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INTESIS_COMMAND_PUSH_WAIT_SECONDS
        while (
            self._device_update_revisions.get(device_id, 0) <= previous_revision
            and loop.time() < deadline
        ):
            await asyncio.sleep(0.05)

        push_received = (
            self._device_update_revisions.get(device_id, 0) > previous_revision
        )
        if push_received:
            self._log(
                f"Intesis command: fresh push state received | device={device_id}",
                level="DEBUG",
            )
            return

        self._log(
            f"Intesis command: push state timeout; retrieving current state | "
            f"device={device_id}",
            level="DEBUG",
        )
        await controller.poll_status(sendcallback=False)
        await self._sync_devices()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return True
        self.connect()
        return bool(self._thread and self._thread.is_alive())

    def _submit(self, coroutine, default=None):
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            self._log(
                "ERRORE Intesis operation: driver event loop is not running"
            )
            return default
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            return future.result(timeout=self.command_timeout_seconds)
        except Exception as exc:
            self._set_error(str(exc))
            self._log(f"ERRORE Intesis operation: {exc}")
            self._log(
                f"TRACEBACK Intesis operation:\n{traceback.format_exc()}",
                level="DEBUG",
            )
            return default

    def _update_connection_state(self):
        controller = self._controller
        connected = bool(controller and controller.is_connected)
        with self._state_lock:
            previous = self._connected
            self._connected = connected
            was_ever_connected = self._ever_connected
            if connected:
                self._ever_connected = True
                self._last_error = None

        if connected and not previous:
            if was_ever_connected:
                self._log("Intesis recovery successful. Normal polling resumed.")
            else:
                self._log("Intesis connection established.")
        elif previous and not connected:
            self._log("Intesis connection lost.")

    def _set_connected(self, connected):
        with self._state_lock:
            self._connected = bool(connected)

    def _set_error(self, message):
        with self._state_lock:
            self._last_error = str(message)

    def _mark_offline(self, error):
        self._update_connection_state()
        with self._state_lock:
            self._last_error = str(error)
            self._failure_count += 1

    def _last_success_label(self):
        with self._state_lock:
            last_success = self._last_success
        if not last_success:
            return "never"
        try:
            return datetime.fromisoformat(last_success).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            return str(last_success)

    def _log_poll_success(self, next_poll_seconds):
        self._log("Intesis polling completed successfully.", level="DEBUG")
        self._log(
            f"Last successful update: {self._last_success_label()}",
            level="DEBUG",
        )
        self._log(
            f"Next poll in {next_poll_seconds} seconds.",
            level="DEBUG",
        )

    def _log_poll_failure(self, next_retry_seconds):
        with self._state_lock:
            failure_count = self._failure_count
        self._log("Intesis polling FAILED.")
        self._log(f"Consecutive failures: {failure_count}")
        self._log(f"Last successful update: {self._last_success_label()}")
        self._log(f"Next retry in {next_retry_seconds} seconds.")

    def _log(self, message, level="INFO"):
        text = str(message)
        if self.log_callback:
            try:
                self.log_callback(text, level=level)
                return
            except TypeError:
                try:
                    self.log_callback(text)
                    return
                except Exception:
                    pass
            except Exception:
                pass
        getattr(logging.getLogger(__name__), level.lower(), logging.info)(text)

    def _safe_get(self, controller, method_name, device_id):
        method = getattr(controller, method_name, None)
        if not callable(method):
            return None
        try:
            return method(device_id)
        except Exception as exc:
            self._log(
                f"Intesis status: reading {method_name} failed for "
                f"{device_id}: {exc}",
                level="DEBUG",
            )
            return None

    @staticmethod
    def _native_number(value):
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _string_list(values):
        if not isinstance(values, (list, tuple, set)):
            return []
        return [str(value).lower() for value in values if value is not None]

    @staticmethod
    def _timestamp():
        return datetime.now().isoformat(timespec="seconds")


_default_lock = threading.RLock()
_default_driver = None


def configure(config=None, log_callback=None, controller_factory=None):
    """Configure the process-wide driver used by the module-level API."""
    global _default_driver
    with _default_lock:
        if _default_driver is not None:
            _default_driver.disconnect()
        _default_driver = IntesisDriver(config, log_callback, controller_factory)
        return _default_driver


def is_configured():
    return _default_driver is not None


def connect():
    return bool(_default_driver and _default_driver.connect())


def disconnect():
    return True if _default_driver is None else _default_driver.disconnect()


def poll():
    return bool(_default_driver and _default_driver.poll())


def get_all_devices():
    return [] if _default_driver is None else _default_driver.get_all_devices()


def get_device(device_id):
    return None if _default_driver is None else _default_driver.get_device(device_id)


def power_on(device_id):
    return bool(_default_driver and _default_driver.power_on(device_id))


def power_off(device_id):
    return bool(_default_driver and _default_driver.power_off(device_id))


def set_mode(device_id, mode):
    return bool(_default_driver and _default_driver.set_mode(device_id, mode))


def set_temperature(device_id, temperature):
    return bool(
        _default_driver and _default_driver.set_temperature(device_id, temperature)
    )


def set_fan_speed(device_id, speed):
    return bool(_default_driver and _default_driver.set_fan_speed(device_id, speed))


def set_vertical_vane(device_id, position):
    return bool(
        _default_driver and _default_driver.set_vertical_vane(device_id, position)
    )


def set_horizontal_vane(device_id, position):
    return bool(
        _default_driver and _default_driver.set_horizontal_vane(device_id, position)
    )
