#!/usr/bin/env python3
"""
Standalone Netatmo Weather Station API diagnostic utility.

This script is intentionally not integrated with the Pool Garden application.
It reads credentials from config/netatmo_config.json, authenticates with
Netatmo OAuth2, retrieves weather station data, and prints a console report.
"""

import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "netatmo_config.json"
TOKENS_PATH = BASE_DIR / "config" / "netatmo_tokens.json"
AUTHORIZE_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"
STATIONS_URL = "https://api.netatmo.com/api/getstationsdata"
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
DEFAULT_SCOPE = "read_station"
REQUIRED_CONFIG_KEYS = ("client_id", "client_secret")


class NetatmoDiagnosticError(Exception):
    """Expected diagnostic failure with a user-facing message."""


def load_config():
    if not CONFIG_PATH.exists():
        raise NetatmoDiagnosticError(
            "Configuration file missing.\n"
            f"Create {CONFIG_PATH} from config/netatmo_config.json.example "
            "and fill in your Netatmo credentials."
        )

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise NetatmoDiagnosticError(f"Invalid JSON in {CONFIG_PATH}: {exc}") from exc

    missing = [key for key in REQUIRED_CONFIG_KEYS if not str(config.get(key, "")).strip()]
    if missing:
        raise NetatmoDiagnosticError(
            "Configuration incomplete. Missing values for: " + ", ".join(missing)
        )

    config["redirect_uri"] = str(config.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
    config["scope"] = str(config.get("scope") or DEFAULT_SCOPE).strip()
    return config


def read_error_body(error):
    try:
        return error.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_api_error(body):
    try:
        data = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return body.strip()

    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return str(message)
    if isinstance(error, str):
        return error
    return data.get("error_description") or data.get("message") or json.dumps(data)


def post_form(url, fields, headers=None):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = read_error_body(exc)
        details = parse_api_error(body)
        raise NetatmoDiagnosticError(
            f"HTTP {exc.code} from {url}: {details or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NetatmoDiagnosticError(f"Network error calling {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise NetatmoDiagnosticError(f"Invalid JSON response from {url}: {exc}") from exc


def get_json(url, access_token):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = read_error_body(exc)
        details = parse_api_error(body)
        raise NetatmoDiagnosticError(
            f"HTTP {exc.code} from {url}: {details or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NetatmoDiagnosticError(f"Network error calling {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise NetatmoDiagnosticError(f"Invalid JSON response from {url}: {exc}") from exc


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


class CallbackConfig:
    redirect_uri = DEFAULT_REDIRECT_URI


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


def wait_for_authorization_code(config, state):
    redirect = urllib.parse.urlparse(config["redirect_uri"])
    if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1"):
        raise NetatmoDiagnosticError(
            "This diagnostic utility expects a local HTTP redirect_uri, for example "
            f"{DEFAULT_REDIRECT_URI}."
        )

    port = redirect.port or 80
    CallbackConfig.redirect_uri = config["redirect_uri"]
    OAuthCallbackHandler.callback_result = {}
    OAuthCallbackHandler.expected_state = state

    server = HTTPServer((redirect.hostname, port), OAuthCallbackHandler)
    try:
        print()
        print("Open this URL in a browser and authorize the Netatmo application:")
        print(build_authorization_url(config, state))
        print()
        print(f"Waiting for OAuth callback on {config['redirect_uri']} ...")
        server.handle_request()
    finally:
        server.server_close()

    result = OAuthCallbackHandler.callback_result
    if result.get("error"):
        raise NetatmoDiagnosticError(f"Authorization failed: {result['error']}")
    if not result.get("code"):
        raise NetatmoDiagnosticError("Authorization failed: no authorization code returned.")
    return result["code"]


def authenticate(config):
    state = secrets.token_urlsafe(24)
    code = wait_for_authorization_code(config, state)
    response = post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "code": code,
            "redirect_uri": config["redirect_uri"],
        },
    )

    token = response.get("access_token")
    if not token:
        raise NetatmoDiagnosticError("Authentication failed: access_token missing.")
    save_tokens(response)
    return token


def save_tokens(response):
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token")
    if not access_token or not refresh_token:
        return
    try:
        expires_in = int(response.get("expires_in", 10800))
    except (TypeError, ValueError):
        expires_in = 10800
    TOKENS_PATH.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": (datetime.now() + timedelta(seconds=max(1, expires_in))).isoformat(timespec="seconds"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def extract_body(response):
    body = response.get("body")
    if not isinstance(body, dict):
        error = response.get("error")
        if error:
            raise NetatmoDiagnosticError(f"Netatmo API error: {error}")
        raise NetatmoDiagnosticError("Netatmo API response did not contain a body.")
    return body


def value_from_dashboard(item, key):
    dashboard = item.get("dashboard_data")
    if not isinstance(dashboard, dict):
        return None
    return dashboard.get(key)


def format_value(value, suffix=""):
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


def format_float(value, suffix="", decimals=1):
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return f"{value}{suffix}"


def format_int(value, suffix=""):
    if value is None:
        return "N/A"
    try:
        return f"{int(round(float(value)))}{suffix}"
    except (TypeError, ValueError):
        return f"{value}{suffix}"


def format_timestamp(value):
    if value is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def last_update(item):
    return (
        value_from_dashboard(item, "time_utc")
        or item.get("last_seen")
        or item.get("last_status_store")
        or item.get("last_setup")
    )


def battery_level(item):
    return item.get("battery_percent", item.get("battery_vp"))


def print_line(label, value):
    print(f"        {label:<12}: {value}")


def print_common_metadata(item, id_label):
    print_line("Module type", format_value(item.get("type")))
    print_line(id_label, format_value(item.get("_id") or item.get("id")))
    print_line("Battery", format_int(battery_level(item), " %"))
    print_line("Last update", format_timestamp(last_update(item)))


def print_station(station):
    print("Station:")
    print(f"    {station.get('station_name') or station.get('module_name') or 'Unnamed station'}")
    print()
    print_line("Temperature", format_float(value_from_dashboard(station, "Temperature"), " °C"))
    print_line("Humidity", format_int(value_from_dashboard(station, "Humidity"), " %"))
    print_line("CO₂", format_int(value_from_dashboard(station, "CO2"), " ppm"))
    print_line("Pressure", format_float(value_from_dashboard(station, "Pressure"), " hPa"))
    print_line("Noise", format_int(value_from_dashboard(station, "Noise"), " dB"))
    print_common_metadata(station, "Station ID")
    print()


def print_module(module):
    module_type = module.get("type")
    module_name = module.get("module_name") or module.get("name") or "Unnamed module"

    print("Module:")
    print(f"    {module_name}")
    print()

    if module_type == "NAModule3":
        print_line("Rain today", format_float(value_from_dashboard(module, "sum_rain_24"), " mm"))
    elif module_type == "NAModule2":
        print_line("Wind speed", format_float(value_from_dashboard(module, "WindStrength"), " km/h"))
        print_line("Gust", format_float(value_from_dashboard(module, "GustStrength"), " km/h"))
        print_line("Direction", format_int(value_from_dashboard(module, "WindAngle"), "°"))
    else:
        print_line("Temperature", format_float(value_from_dashboard(module, "Temperature"), " °C"))
        print_line("Humidity", format_int(value_from_dashboard(module, "Humidity"), " %"))
        print_line("CO₂", format_int(value_from_dashboard(module, "CO2"), " ppm"))

    print_common_metadata(module, "Module ID")
    print()


def group_stations_by_home(devices):
    homes = {}
    for station in devices:
        home_id = station.get("home_id") or "unknown-home"
        home_name = station.get("home_name") or station.get("place", {}).get("city") or "Unknown Home"
        home = homes.setdefault(home_id, {"name": home_name, "stations": []})
        home["stations"].append(station)
    return homes


def print_report(stations_response):
    body = extract_body(stations_response)
    devices = body.get("devices")
    if not isinstance(devices, list) or not devices:
        raise NetatmoDiagnosticError("No Netatmo Weather Stations were returned for this account.")

    homes = group_stations_by_home(devices)
    for home in homes.values():
        print("=" * 49)
        print(f"HOME: {home['name']}")
        print("=" * 49)
        print()

        for station in home["stations"]:
            print_station(station)
            for module in station.get("modules") or []:
                print_module(module)


def main():
    try:
        config = load_config()
        access_token = authenticate(config)
        stations_response = get_json(STATIONS_URL, access_token)
        print_report(stations_response)
    except NetatmoDiagnosticError as exc:
        print(str(exc), file=sys.stderr)
        print()
        print("Netatmo communication failed.")
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        print()
        print("Netatmo communication failed.")
        return 1

    print("Netatmo communication successful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
