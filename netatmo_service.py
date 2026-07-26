import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional


AUTHORIZE_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"
STATIONS_URL = "https://api.netatmo.com/api/getstationsdata"
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
DEFAULT_SCOPE = "read_station"
TOKEN_REFRESH_MARGIN_SECONDS = 60


class NetatmoServiceError(Exception):
    pass


class NetatmoHttpError(NetatmoServiceError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class NetatmoAuthorizationRequired(NetatmoServiceError):
    pass


class CallbackConfig:
    redirect_uri = DEFAULT_REDIRECT_URI


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    callback_result = {}
    expected_state = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != urllib.parse.urlparse(CallbackConfig.redirect_uri).path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Unknown callback path.")
            return

        state = params.get("state", [""])[0]
        if state != self.expected_state:
            type(self).callback_result = {"error": "Invalid OAuth state returned by Netatmo."}
        elif "error" in params:
            type(self).callback_result = {
                "error": params.get("error_description", params.get("error", ["OAuth error"]))[0]
            }
        else:
            type(self).callback_result = {"code": params.get("code", [""])[0]}

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"Netatmo authorization received. You can close this browser tab and return to the terminal."
        )

    def log_message(self, format, *args):
        return


def build_authorization_url(config, state):
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "scope": config["scope"],
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def wait_for_authorization_code(config, state, log_callback=None):
    redirect = urllib.parse.urlparse(config["redirect_uri"])
    if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1"):
        raise NetatmoAuthorizationRequired(
            "This diagnostic utility expects a local HTTP redirect_uri, for example "
            f"{DEFAULT_REDIRECT_URI}."
        )

    def log(message):
        if log_callback:
            log_callback(message)
        else:
            print(message)

    port = redirect.port or 80
    CallbackConfig.redirect_uri = config["redirect_uri"]
    OAuthCallbackHandler.callback_result = {}
    OAuthCallbackHandler.expected_state = state

    server = HTTPServer((redirect.hostname, port), OAuthCallbackHandler)
    try:
        log(f"Netatmo OAuth callback HTTP server started on {redirect.hostname}:{port}")
        log("")
        log("Open this URL in a browser and authorize the Netatmo application:")
        log(build_authorization_url(config, state))
        log("")
        log(f"Waiting for OAuth callback on {config['redirect_uri']} ...")
        server.handle_request()
    finally:
        server.server_close()

    result = OAuthCallbackHandler.callback_result
    if result.get("error"):
        raise NetatmoAuthorizationRequired(f"Authorization failed: {result['error']}")
    if not result.get("code"):
        raise NetatmoAuthorizationRequired("Authorization failed: no authorization code returned.")
    log("Netatmo authorization code received")
    return result["code"]


class NetatmoService:
    def __init__(
        self,
        config,
        credentials_path,
        tokens_path,
        rain_history_path=None,
        event_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
    ):
        config = config or {}
        self.enabled = bool(config.get("enabled", True))
        self.poll_seconds = max(60, int(config.get("poll_seconds", 300)))
        self.retry_seconds = max(10, int(config.get("retry_seconds", 60)))
        self.timeout_seconds = float(config.get("timeout_seconds", 30))
        self.credentials_path = Path(config.get("credentials_path") or credentials_path)
        self.tokens_path = Path(config.get("tokens_path") or tokens_path)
        self.rain_history_path = Path(
            config.get("rain_history_path") or rain_history_path or self.tokens_path.with_name("netatmo_rain_history.json")
        )
        self.event_callback = event_callback
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._rain_history = self._load_rain_history()
        self._state = self._initial_state()

    def start(self):
        if not self.enabled:
            self._log("Netatmo service disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("Netatmo service started")

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            snapshot = json.loads(json.dumps(self._state))
        self._update_rain_days(snapshot.get("stations", {}))
        self._update_rain_days(snapshot.get("modules", {}))
        return snapshot

    def _initial_state(self):
        return {
            "enabled": self.enabled,
            "status": "starting" if self.enabled else "disabled",
            "online": False,
            "authenticated": False,
            "authorization_required": False,
            "last_successful_poll": None,
            "last_attempt": None,
            "last_error": None,
            "poll_seconds": self.poll_seconds,
            "homes": {},
            "stations": {},
            "modules": {},
            "locations": {},
        }

    def _run(self):
        while not self._stop.is_set():
            next_wait = self.poll_seconds
            try:
                self.poll_once()
            except Exception as exc:
                next_wait = self.retry_seconds
                self._mark_failed(exc)
                self._log(f"Netatmo poll failed: {exc} (retry in {self.retry_seconds} seconds)")
            self._stop.wait(next_wait)

    def poll_once(self):
        attempted_at = self._now_iso()
        with self._lock:
            self._state["last_attempt"] = attempted_at
        access_token = self.access_token()
        self._log("Polling Netatmo...", level="DEBUG")
        response = self._get_json(STATIONS_URL, access_token)
        self._debug_log_json("Netatmo getstationsdata raw response", response)
        snapshot = self._parse_stations_response(response, attempted_at)
        self._record_rain_days(snapshot)
        with self._lock:
            self._state.update(snapshot)
            self._state.update({
                "status": "online",
                "online": True,
                "authenticated": True,
                "authorization_required": False,
                "last_successful_poll": attempted_at,
                "last_error": None,
            })
            final_snapshot = json.loads(json.dumps(self._state))
        self._debug_log_json("NetatmoService final snapshot stored", final_snapshot)
        self._log("Netatmo poll successful", level="DEBUG")

    def access_token(self):
        credentials = self._load_credentials()
        tokens = self._load_tokens()
        access_token = tokens.get("access_token")
        expires_at = self._parse_datetime(tokens.get("expires_at"))
        if access_token and expires_at and expires_at > datetime.now() + timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS):
            self._log("Using cached access token", level="DEBUG")
            with self._lock:
                self._state.update({"authenticated": True, "authorization_required": False})
            return access_token

        if tokens.get("refresh_token"):
            self._log("Refreshing expired access token", level="DEBUG")
            try:
                refreshed = self._refresh_access_token(credentials, tokens["refresh_token"])
                self._save_tokens(refreshed)
                with self._lock:
                    self._state.update({"authenticated": True, "authorization_required": False})
                return refreshed["access_token"]
            except NetatmoHttpError as exc:
                if exc.status_code not in (400, 401, 403):
                    raise
                self._log(f"Netatmo refresh token invalid or revoked: {exc}", level="DEBUG")
            except Exception as exc:
                raise NetatmoServiceError(f"Netatmo token refresh failed: {exc}") from exc

        self._log("Netatmo authorization required")
        with self._lock:
            self._state.update({"authenticated": False, "authorization_required": True})
        authorized = self._authorize(credentials)
        self._save_tokens(authorized)
        self._log(f"Netatmo tokens saved to {self.tokens_path}", level="DEBUG")
        with self._lock:
            self._state.update({"authenticated": True, "authorization_required": False})
        return authorized["access_token"]

    def _load_credentials(self):
        if not self.credentials_path.exists():
            raise NetatmoServiceError(
                f"Netatmo credentials file missing: {self.credentials_path}"
            )
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NetatmoServiceError(f"Invalid Netatmo credentials JSON: {exc}") from exc
        for key in ("client_id", "client_secret"):
            if not str(data.get(key) or "").strip():
                raise NetatmoServiceError(f"Netatmo credentials missing {key}")
        data["redirect_uri"] = str(data.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
        data["scope"] = str(data.get("scope") or DEFAULT_SCOPE).strip()
        return data

    def _load_tokens(self):
        if not self.tokens_path.exists():
            return {}
        try:
            return json.loads(self.tokens_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NetatmoServiceError(f"Invalid Netatmo token JSON: {exc}") from exc

    def _save_tokens(self, tokens):
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_path.write_text(
            json.dumps(
                {
                    "access_token": tokens.get("access_token"),
                    "refresh_token": tokens.get("refresh_token"),
                    "expires_at": tokens.get("expires_at"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _load_rain_history(self):
        if not self.rain_history_path.exists():
            return {}
        try:
            data = json.loads(self.rain_history_path.read_text(encoding="utf-8"))
            dates = data.get("last_rain_dates", {}) if isinstance(data, dict) else {}
            return dates if isinstance(dates, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"Netatmo rain history could not be loaded: {exc}")
            return {}

    def _save_rain_history(self):
        self.rain_history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.rain_history_path.with_suffix(self.rain_history_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps({"last_rain_dates": self._rain_history}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(self.rain_history_path)

    def _record_rain_days(self, snapshot):
        today = datetime.now().date().isoformat()
        changed = False
        for devices_key in ("stations", "modules"):
            for device_id, device in snapshot.get(devices_key, {}).items():
                rain = (device.get("metrics") or {}).get("rain_today") or {}
                try:
                    rain_value = float(rain.get("value"))
                except (TypeError, ValueError):
                    continue
                if rain_value > 0 and self._rain_history.get(device_id) != today:
                    self._rain_history[device_id] = today
                    changed = True
        if changed:
            self._save_rain_history()
        self._update_rain_days(snapshot.get("stations", {}))
        self._update_rain_days(snapshot.get("modules", {}))

    def _update_rain_days(self, devices):
        today = datetime.now().date()
        for device_id, device in devices.items():
            if "rain_today" not in (device.get("metrics") or {}):
                continue
            last_rain_date = self._rain_history.get(device_id)
            device["last_rain_date"] = last_rain_date
            device["days_since_last_rain"] = None
            if not last_rain_date:
                continue
            try:
                device["days_since_last_rain"] = max(0, (today - datetime.fromisoformat(last_rain_date).date()).days)
            except (TypeError, ValueError):
                device["last_rain_date"] = None

    def _refresh_access_token(self, credentials, refresh_token):
        response = self._post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
                "refresh_token": refresh_token,
            },
        )
        return self._token_payload(response, fallback_refresh_token=refresh_token)

    def _authorize(self, credentials):
        state = secrets.token_urlsafe(24)
        code = wait_for_authorization_code(credentials, state, log_callback=lambda message: self._log(message, level="DEBUG"))
        self._log("Exchanging Netatmo authorization code for tokens", level="DEBUG")
        response = self._post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
                "code": code,
                "redirect_uri": credentials["redirect_uri"],
            },
        )
        self._log("Netatmo token exchange successful", level="DEBUG")
        return self._token_payload(response)

    def _token_payload(self, response, fallback_refresh_token=None):
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token") or fallback_refresh_token
        if not access_token or not refresh_token:
            raise NetatmoServiceError("Netatmo token response is missing access_token or refresh_token")
        try:
            expires_in = int(response.get("expires_in", 10800))
        except (TypeError, ValueError):
            expires_in = 10800
        expires_at = datetime.now() + timedelta(seconds=max(1, expires_in))
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    def _parse_stations_response(self, response, timestamp):
        body = response.get("body")
        if not isinstance(body, dict):
            raise NetatmoServiceError("Netatmo response did not contain a body")
        devices = body.get("devices")
        if not isinstance(devices, list):
            raise NetatmoServiceError("Netatmo response did not contain devices")

        homes = {}
        stations = {}
        modules = {}
        locations = {}
        for station in devices:
            home_id = str(station.get("home_id") or station.get("_id") or "unknown")
            home_name = station.get("home_name") or (station.get("place") or {}).get("city") or "Unknown Home"
            self._log(f"Netatmo debug home found: id={home_id} name={home_name}", level="DEBUG")
            home = homes.setdefault(home_id, {
                "id": home_id,
                "name": home_name,
                "stations": [],
            })
            station_data = self._station_snapshot(station, timestamp)
            self._log_device_debug("station", station_data)
            home["stations"].append(station_data["id"])
            stations[station_data["id"]] = station_data
            locations.setdefault(home_id, {"label": home_name, "metrics": {}})
            self._merge_location_metrics(locations[home_id]["metrics"], station_data)

            for module in station.get("modules") or []:
                module_data = self._module_snapshot(module, station_data["id"], home_id, home_name, timestamp)
                self._log_device_debug("module", module_data)
                station_data["modules"].append(module_data["id"])
                modules[module_data["id"]] = module_data
                self._merge_location_metrics(locations[home_id]["metrics"], module_data)

        return {
            "homes": homes,
            "stations": stations,
            "modules": modules,
            "locations": locations,
        }

    def _station_snapshot(self, station, timestamp):
        return {
            "id": str(station.get("_id") or station.get("id") or ""),
            "name": station.get("station_name") or station.get("module_name") or "Unnamed station",
            "type": station.get("type"),
            "home_id": str(station.get("home_id") or "unknown"),
            "home_name": station.get("home_name") or (station.get("place") or {}).get("city") or "Unknown Home",
            "battery": station.get("battery_percent", station.get("battery_vp")),
            "last_update": self._last_update(station),
            "online": True,
            "updated_at": timestamp,
            "modules": [],
            "metrics": self._metrics_snapshot(station),
        }

    def _module_snapshot(self, module, station_id, home_id, home_name, timestamp):
        return {
            "id": str(module.get("_id") or module.get("id") or ""),
            "station_id": station_id,
            "name": module.get("module_name") or module.get("name") or "Unnamed module",
            "type": module.get("type"),
            "home_id": home_id,
            "home_name": home_name,
            "battery": module.get("battery_percent", module.get("battery_vp")),
            "last_update": self._last_update(module),
            "online": True,
            "updated_at": timestamp,
            "metrics": self._metrics_snapshot(module),
        }

    def _metrics_snapshot(self, item):
        dashboard = item.get("dashboard_data")
        if not isinstance(dashboard, dict):
            dashboard = {}
        return {
            "temperature": self._metric(dashboard.get("Temperature"), "Temperature", "°C"),
            "humidity": self._metric(dashboard.get("Humidity"), "Humidity", "%"),
            "co2": self._metric(dashboard.get("CO2"), "CO2", "ppm"),
            "pressure": self._metric(dashboard.get("Pressure"), "Pressure", "hPa"),
            "noise": self._metric(dashboard.get("Noise"), "Noise", "dB"),
            "rain_today": self._metric(dashboard.get("sum_rain_24"), "Rain today", "mm"),
            "wind_speed": self._metric(dashboard.get("WindStrength"), "Wind speed", "km/h"),
            "gust": self._metric(dashboard.get("GustStrength"), "Gust", "km/h"),
            "direction": self._metric(dashboard.get("WindAngle"), "Direction", "°"),
        }

    def _metric(self, value, label, unit):
        return {
            "label": label,
            "unit": unit,
            "value": value,
            "online": value is not None,
        }

    def _merge_location_metrics(self, target, device):
        prefix = self._slug(device.get("name") or device.get("id") or "device")
        for metric_key, metric in device.get("metrics", {}).items():
            if metric.get("value") is None:
                continue
            target[f"{prefix}_{metric_key}"] = {
                **metric,
                "label": f"{device.get('name')} {metric.get('label')}",
                "source": "netatmo",
                "device_id": device.get("id"),
                "device_name": device.get("name"),
                "module_type": device.get("type"),
                "last_valid_update": device.get("last_update"),
                "last_attempt": device.get("updated_at"),
                "placeholder": False,
            }

    def _last_update(self, item):
        dashboard = item.get("dashboard_data")
        if isinstance(dashboard, dict) and dashboard.get("time_utc") is not None:
            return self._format_timestamp(dashboard.get("time_utc"))
        for key in ("last_seen", "last_status_store", "last_setup"):
            if item.get(key) is not None:
                return self._format_timestamp(item.get(key))
        return None

    def _post_form(self, url, fields):
        self._log(
            f"Netatmo OAuth request: grant_type={fields.get('grant_type')} url={url}",
            level="DEBUG"
        )
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        return self._open_json(request, url)

    def _get_json(self, url, access_token):
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            method="GET",
        )
        return self._open_json(request, url)

    def _open_json(self, request, url):
        try:
            self._log(f"Netatmo HTTP {request.get_method()} {url}", level="DEBUG")
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = self._read_error_body(exc)
            details = self._parse_api_error(body) or exc.reason
            raise NetatmoHttpError(exc.code, f"HTTP {exc.code} from {url}: {details}") from exc
        except urllib.error.URLError as exc:
            raise NetatmoServiceError(f"Network error calling {url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise NetatmoServiceError(f"Invalid JSON response from {url}: {exc}") from exc

    def _mark_failed(self, error):
        with self._lock:
            self._state.update({
                "status": "offline",
                "online": False,
                "last_attempt": self._now_iso(),
                "last_error": str(error),
            })

    def _log_device_debug(self, kind, device):
        self._log(
            "Netatmo debug "
            f"{kind} found: id={device.get('id')} name={device.get('name')} "
            f"type={device.get('type')} home={device.get('home_name')} "
            f"station_id={device.get('station_id', 'N/A')} "
            f"battery={device.get('battery')} last_update={device.get('last_update')}",
            level="DEBUG"
        )
        for metric_key, metric in (device.get("metrics") or {}).items():
            self._log(
                "Netatmo debug measured value: "
                f"{kind}={device.get('name')} metric={metric_key} "
                f"label={metric.get('label')} value={metric.get('value')} "
                f"unit={metric.get('unit')} online={metric.get('online')}",
                level="DEBUG"
            )

    def _debug_log_json(self, title, data):
        try:
            formatted = json.dumps(data, indent=2, ensure_ascii=False)
        except TypeError:
            formatted = str(data)
        self._log(f"{title}:\n{formatted}", level="DEBUG")

    def _record_event(self, event, reason):
        if self.event_callback:
            self.event_callback("Netatmo", "Weather Stations", event, "Netatmo Service", reason)

    def _log(self, message, level="INFO"):
        if self.log_callback:
            try:
                self.log_callback(message, level=level)
            except TypeError:
                self.log_callback(message)
        else:
            print(message)

    @staticmethod
    def _parse_datetime(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _format_timestamp(value):
        try:
            return datetime.fromtimestamp(int(value)).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            return str(value) if value is not None else None

    @staticmethod
    def _read_error_body(error):
        try:
            return error.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _parse_api_error(body):
        try:
            data = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return str(body or "").strip()
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or json.dumps(error)
        if isinstance(error, str):
            return data.get("error_description") or error
        return data.get("message") or json.dumps(data)

    @staticmethod
    def _slug(value):
        text = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "device"))
        return "_".join(part for part in text.split("_") if part) or "device"

    @staticmethod
    def _now_iso():
        return datetime.now().isoformat(timespec="seconds")


DEFAULT_NETATMO_CONFIG: Dict[str, object] = {
    "enabled": True,
    "poll_seconds": 300,
    "retry_seconds": 60,
    "timeout_seconds": 30,
}
