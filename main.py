#!/usr/bin/env python
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import re
import shlex
import signal
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


MAILTM_BASE_URL = "https://api.mail.tm"
MAILTM_RETRY_DELAY_S = 10.0

STOP_REQUESTED = False


def _sigint_handler(signum, frame) -> None:  # type: ignore[no-untyped-def]
    global STOP_REQUESTED
    if STOP_REQUESTED:
        raise KeyboardInterrupt
    STOP_REQUESTED = True
    try:
        sys.stderr.write("\nSIGINT received. Stopping after current action... (press Ctrl+C again to force)\n")
        sys.stderr.flush()
    except Exception:
        pass


def install_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except Exception:
        pass


def sleep_interruptible(seconds: float) -> None:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if STOP_REQUESTED:
            raise KeyboardInterrupt
        time.sleep(0.05)


def _run(argv: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _adb_base(adb_path: str, serial: Optional[str]) -> List[str]:
    base = [adb_path]
    if serial:
        base += ["-s", serial]
    return base


def adb_devices(adb_path: str) -> str:
    return _run([adb_path, "devices"]).stdout


def prompt_adb_path(default: str) -> str:
    return default


def pick_serial(adb_path: str, serial: Optional[str]) -> str:
    if serial:
        return serial
    env_serial = (os.environ.get("ADB_SERIAL") or "").strip()
    if env_serial:
        return env_serial
    out = adb_devices(adb_path)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.lower().startswith("list of devices")]
    devices = [ln.split()[0] for ln in lines if "\tdevice" in ln]
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise SystemExit(f"No adb device. Output:\n{out}")

    default_choice = devices[0]
    preferred = ["emulator-5554"]
    for p in preferred:
        if p in devices:
            default_choice = p
            break
    else:
        for d in devices:
            if d.startswith("127.0.0.1:"):
                default_choice = d
                break
        else:
            for d in devices:
                if d.startswith("emulator-"):
                    default_choice = d
                    break

    print("Multiple adb devices terdeteksi:")
    for idx, dev in enumerate(devices, 1):
        marker = " (default)" if dev == default_choice else ""
        print(f"  {idx}. {dev}{marker}")
    prompt = f"Pilih nomor device (1-{len(devices)}) atau ketik serial [{default_choice}]: "
    try:
        resp = input(prompt).strip()
    except EOFError:
        resp = ""
    if not resp:
        return default_choice
    if resp.isdigit():
        num = int(resp)
        if 1 <= num <= len(devices):
            return devices[num - 1]
    return resp


def adb_shell(adb_path: str, serial: str, cmd: str) -> str:
    argv = _adb_base(adb_path, serial) + ["shell"] + shlex.split(cmd)
    cp = _run(argv, check=True)
    return (cp.stdout or "").strip()


def adb_tap(adb_path: str, serial: str, x: int, y: int) -> None:
    _run(_adb_base(adb_path, serial) + ["shell", "input", "tap", str(x), str(y)], check=True)


def adb_swipe(adb_path: str, serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
    _run(
        _adb_base(adb_path, serial)
        + ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(max(1, int(duration_ms)))],
        check=True,
    )


def adb_text(adb_path: str, serial: str, text: str) -> None:
    _run(_adb_base(adb_path, serial) + ["shell", "input", "text", text], check=True)


def wait_enter(prompt: str) -> None:
    try:
        sys.stdout.write(str(prompt))
        if not str(prompt).endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.write("(Klik/focus ke jendela CMD ini, lalu tekan Enter)\n")
        sys.stdout.flush()
        input()
    except EOFError:
        return


def wait_enter_or_timeout(prompt: str, timeout_s: float) -> None:
    if timeout_s <= 0:
        wait_enter(prompt)
        return
    if os.name != "nt":
        wait_enter(prompt)
        return

    import msvcrt  # type: ignore

    sys.stdout.write(str(prompt))
    if not str(prompt).endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.write(f"(Tekan Enter untuk lanjut, atau tunggu {timeout_s:.0f} detik...)\n")
    sys.stdout.flush()

    deadline = time.time() + timeout_s
    buf = b""
    while time.time() < deadline:
        if STOP_REQUESTED:
            raise KeyboardInterrupt
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            buf += ch
            if ch in (b"\r", b"\n"):
                return
        time.sleep(0.05)


def _http_json(method: str, url: str, *, headers: Optional[Dict[str, str]] = None, body: Optional[Dict[str, Any]] = None) -> Any:
    data = None
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {raw}") from e


class MailTm:
    def __init__(self, base_url: str = MAILTM_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def domains(self) -> List[str]:
        j = _http_json("GET", f"{self.base_url}/domains")
        if isinstance(j, list):
            members = j
        elif isinstance(j, dict):
            members = j.get("hydra:member") or j.get("member") or j.get("domains") or []
        else:
            members = []

        domains: List[str] = []
        for d in members:
            if not isinstance(d, dict):
                continue
            dom = (d.get("domain") or d.get("name") or "").strip()
            is_active = bool(d.get("isActive", True))
            is_private = bool(d.get("isPrivate", False))
            if dom and is_active and not is_private:
                domains.append(dom)
        return domains

    def create_account(self, address: str, password: str) -> Dict[str, Any]:
        return _http_json("POST", f"{self.base_url}/accounts", body={"address": address, "password": password})

    def get_token(self, address: str, password: str) -> str:
        j = _http_json("POST", f"{self.base_url}/token", body={"address": address, "password": password})
        tok = (j or {}).get("token")
        if not tok:
            raise RuntimeError(f"Unexpected token response: {j}")
        self.token = str(tok)
        return self.token

    def list_messages(self) -> List[Dict[str, Any]]:
        j = _http_json("GET", f"{self.base_url}/messages", headers=self._auth_headers())
        if isinstance(j, list):
            members = j
        elif isinstance(j, dict):
            members = j.get("hydra:member") or j.get("member") or j.get("messages") or []
        else:
            members = []
        return [m for m in members if isinstance(m, dict)]

    def get_message(self, msg_id: str) -> Dict[str, Any]:
        return _http_json("GET", f"{self.base_url}/messages/{msg_id}", headers=self._auth_headers())


@dataclasses.dataclass
class OtpFuture:
    event: threading.Event
    code: Optional[str] = None
    error: Optional[BaseException] = None

    def get(self, timeout_s: Optional[float] = None) -> str:
        ok = self.event.wait(timeout_s)
        if not ok:
            raise TimeoutError("OTP future timed out.")
        if self.error:
            raise RuntimeError(f"OTP fetch failed: {self.error}") from self.error
        if not self.code:
            raise TimeoutError("OTP not found.")
        return self.code


def start_otp_prefetch(
    *,
    mailtm_module: Any,
    token: str,
    timeout_s: int,
    poll_s: float,
    otp_regex: str,
) -> OtpFuture:
    fut = OtpFuture(event=threading.Event())

    def worker() -> None:
        try:
            code = mailtm_module.wait_for_otp(token, timeout_s=timeout_s, poll_s=poll_s, regex=otp_regex)
            fut.code = code
        except BaseException as e:
            fut.error = e
        finally:
            fut.event.set()

    t = threading.Thread(target=worker, name="otp-prefetch", daemon=True)
    t.start()
    return fut


def gen_address(domains: List[str]) -> str:
    if not domains:
        raise RuntimeError("No mail.tm domains available.")
    domain = random.choice(domains)
    local = "u" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return f"{local}@{domain}"


def wait_for_otp(
    mail: MailTm,
    *,
    otp_regex: str,
    timeout_s: int,
    poll_s: float,
) -> str:
    deadline = time.time() + timeout_s
    rx = re.compile(otp_regex)
    seen: set[str] = set()

    def flatten_message_text(full: Dict[str, Any], summary: Dict[str, Any]) -> str:
        parts: List[str] = []

        def add(v: Any) -> None:
            if v is None:
                return
            if isinstance(v, str):
                s = v
            elif isinstance(v, (list, tuple)):
                s = "\n".join(str(x) for x in v if x is not None)
            else:
                s = str(v)
            s = s.strip()
            if s:
                parts.append(s)

        add(full.get("text"))
        add(full.get("html"))
        add(full.get("intro"))
        add(full.get("subject"))
        add(summary.get("intro"))
        add(summary.get("subject"))
        return "\n".join(parts)

    while time.time() < deadline:
        if STOP_REQUESTED:
            raise KeyboardInterrupt
        msgs = mail.list_messages()
        msgs.sort(key=lambda m: (m.get("createdAt") or ""), reverse=True)
        for m in msgs:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            full = mail.get_message(mid)
            blob = flatten_message_text(full, m)
            m2 = rx.search(blob)
            if m2:
                return m2.group(1)
        time.sleep(poll_s)

    raise TimeoutError(f"OTP not found within {timeout_s}s.")


@dataclasses.dataclass
class Step:
    kind: str
    x: Optional[int] = None
    y: Optional[int] = None
    seconds: Optional[float] = None
    meta: Optional[Dict[str, Any]] = None


def run_steps(
    *,
    adb_path: str,
    serial: str,
    steps: List[Step],
    email: str,
    otp: str,
    referral: str,
    password: str,
    dry_run: bool,
    mail: Optional[MailTm] = None,
    otp_regex: str = r"\b(\d{6})\b",
    otp_timeout_s: int = 180,
    otp_poll_s: float = 2.0,
    after_tap_delay_s: float = 0.50,
    after_text_delay_s: float = 0.50,
    implicit_delay_s: float = 1.0,
    otp_future: Optional[OtpFuture] = None,
    enter_timeout_s: float = 0.0,
    verbose: bool = True,
    scroll_count: int = 2,
    scroll_duration_ms: int = 450,
    scroll_pause_s: float = 0.35,
) -> None:
    otp_value = otp

    def describe(step: Step, default: str) -> str:
        meta = step.meta or {}
        label = meta.get("label")
        if label:
            return str(label)
        return default

    def ensure_otp(reason: Optional[str] = None) -> str:
        nonlocal otp_value
        if otp_value:
            return otp_value
        if dry_run:
            if not otp_value:
                otp_value = "123456"
            return otp_value
        try:
            if otp_future is not None:
                msg = reason or "OTP: waiting (prefetch)..."
                if verbose and msg:
                    print(msg)
                otp_value = otp_future.get(timeout_s=float(otp_timeout_s) + 10.0)
            else:
                if not mail:
                    raise RuntimeError("OTP needed but mail client is missing.")
                if reason:
                    print(reason)
                else:
                    print("Menunggu OTP masuk ke inbox mail.tm ...")
                otp_value = wait_for_otp(mail, otp_regex=otp_regex, timeout_s=otp_timeout_s, poll_s=otp_poll_s)
        except Exception as e:
            print(f"OTP tidak kebaca otomatis: {e}", file=sys.stderr)
            otp_value = input("Masukkan OTP manual: ").strip()
        return otp_value

    for s in steps:
        if STOP_REQUESTED:
            raise KeyboardInterrupt
        meta = s.meta or {}
        silent = bool(meta.get("silent"))
        if s.kind == "tap":
            desc = describe(s, f"TAP: {s.x},{s.y}")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_tap(adb_path, serial, int(s.x), int(s.y))
                implicit = bool(meta.get("implicit_delay", False))
                delay = float(after_tap_delay_s)
                if implicit:
                    delay = max(delay, float(implicit_delay_s))
                if delay > 0:
                    sleep_interruptible(delay)
        elif s.kind == "text_email":
            desc = describe(s, "TEXT: email")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_text(adb_path, serial, email)
                if after_text_delay_s > 0:
                    sleep_interruptible(after_text_delay_s)
        elif s.kind == "text_otp":
            otp_value = ensure_otp()
            desc = describe(s, "TEXT: otp")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_text(adb_path, serial, otp_value)
                if after_text_delay_s > 0:
                    sleep_interruptible(after_text_delay_s)
        elif s.kind == "text_ref":
            desc = describe(s, "TEXT: referral")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_text(adb_path, serial, referral)
                if after_text_delay_s > 0:
                    sleep_interruptible(after_text_delay_s)
        elif s.kind == "text_pass":
            desc = describe(s, "TEXT: password")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_text(adb_path, serial, password)
                if after_text_delay_s > 0:
                    sleep_interruptible(after_text_delay_s)
        elif s.kind == "wait":
            sec = float(s.seconds or 0)
            desc = describe(s, f"WAIT: {sec:.1f}s")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                sleep_interruptible(sec)
        elif s.kind == "wait_enter":
            prompt = (meta.get("prompt") or "Press Enter to continue...")
            if dry_run:
                print(f"WAIT_ENTER: {prompt}")
            else:
                wait_enter_or_timeout(prompt, float(enter_timeout_s))
        elif s.kind == "wait_otp_ready":
            prompt = (meta.get("prompt") or "OTP: menunggu kode masuk sebelum lanjut...")
            desc = describe(s, str(prompt))
            if dry_run:
                print(desc)
            else:
                if verbose and not silent and desc:
                    print(desc)
                ensure_otp(prompt if isinstance(prompt, str) else None)
        elif s.kind == "scroll":
            x1, y1, x2, y2 = 360, 1050, 360, 260
            desc = describe(s, f"SCROLL: swipe up x{x1} y{y1} -> x{x2} y{y2} (x{scroll_count})")
            if verbose and not silent:
                print(desc)
            if dry_run:
                continue
            for _ in range(max(1, int(scroll_count))):
                adb_swipe(adb_path, serial, x1, y1, x2, y2, int(scroll_duration_ms))
                if scroll_pause_s > 0:
                    sleep_interruptible(float(scroll_pause_s))
        elif s.kind == "loop":
            if dry_run:
                print("LOOP_MARKER")
        elif s.kind == "swipe":
            desc = describe(s, f"SWIPE {s.meta}")
            if dry_run:
                print(desc)
            else:
                if verbose and not silent:
                    print(desc)
                adb_swipe(
                    adb_path,
                    serial,
                    int(meta["x1"]),
                    int(meta["y1"]),
                    int(meta["x2"]),
                    int(meta["y2"]),
                    int(meta.get("duration_ms", 300)),
                )

XY_RE = re.compile(r"x\s*[:;]?\s*(\d+)\s*y\s*[:;]?\s*(\d+)", re.IGNORECASE)
WAIT_RE = re.compile(r"tunggu\s+(\d+)\s*detik", re.IGNORECASE)
STEP_NUM_RE = re.compile(r"^\s*(\d+)")
STEP_PREFIX_RE = re.compile(r"^\s*\d+[\).\s-]*", re.IGNORECASE)


def make_meta(line: str, *, label: Optional[str] = None, silent: bool = False, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"raw": line}
    if extra:
        meta.update(extra)
    if label:
        meta["label"] = label
    if silent:
        meta["silent"] = True
    return meta


def build_label(line: str, step_no: Optional[int]) -> Optional[str]:
    desc = STEP_PREFIX_RE.sub("", line, count=1)
    desc = XY_RE.sub("", desc)
    desc = " ".join(desc.strip().split())
    if not desc:
        return None
    if step_no is not None:
        return f"Langkah {step_no}: {desc}"
    return desc


def parse_kordinat2_file(path: str) -> Tuple[List[Step], bool]:
    text = open(path, "r", encoding="utf-8").read().splitlines()
    steps: List[Step] = []
    has_loop = False

    for raw in text:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        step_no = None
        mnum = STEP_NUM_RE.match(line)
        if mnum:
            try:
                step_no = int(mnum.group(1))
            except ValueError:
                step_no = None

        label = build_label(line, step_no)

        if "back to no 1" in low or "back to no. 1" in low or "back to nomor 1" in low:
            steps.append(Step(kind="loop"))
            has_loop = True
            continue

        if "sekrol" in low or "scroll" in low:
            meta = make_meta(line, label=label, silent=True)
            steps.append(Step(kind="scroll", meta=meta))
            continue

        mxy = XY_RE.search(line.replace(",", " "))
        if not mxy:
            continue
        x, y = int(mxy.group(1)), int(mxy.group(2))

        waits = [int(m) for m in WAIT_RE.findall(line)]
        pre_waits: List[int] = []
        post_waits: List[int] = []

        if waits:
            if "sblm" in low or "sebelum" in low:
                pre_waits = waits
            else:
                post_waits = waits

        for w in pre_waits:
            steps.append(Step(kind="wait", seconds=float(w), meta=make_meta(line, silent=True)))

        implicit_ok = True
        if "tunggu" in low or "captcha" in low or "polling otp" in low:
            implicit_ok = False

        tap_label = None
        tap_silent = True
        if step_no == 5:
            tap_label = "Selesaikan CAPTCHA manual, script lanjut otomatis."
            tap_silent = False
        tap_meta = make_meta(line, label=tap_label or label, silent=tap_silent, extra={"implicit_delay": implicit_ok})
        steps.append(Step(kind="tap", x=x, y=y, meta=tap_meta))

        if "polling otp" in low or "otp baru lanjut" in low:
            prompt = "OTP: lagi dicek sambil kamu selesaikan captcha..."
            meta = make_meta(line, label="Menunggu OTP dari mail.tm", extra={"prompt": prompt})
            steps.append(Step(kind="wait_otp_ready", meta=meta))

        if step_no == 14:
            steps.append(
                Step(
                    kind="wait",
                    seconds=2.0,
                    meta=make_meta(
                        "Extra jeda setelah step 14",
                        label="Menstabilkan layar setelah konfirmasi",
                        silent=True,
                    ),
                )
            )

        if "isi email" in low:
            meta = make_meta(line, label="Mengisi email")
            steps.append(Step(kind="text_email", meta=meta))
        if "isi otp" in low:
            meta = make_meta(line, label="OTP ditemukan, memasukkan kode OTP")
            steps.append(Step(kind="text_otp", meta=meta))
        if "kode referral" in low or "kode reff" in low or "kode ref" in low:
            meta = make_meta(line, label="Mengisi kode referral")
            steps.append(Step(kind="text_ref", meta=meta))
        if "retype password" in low or "ulang password" in low:
            meta = make_meta(line, label="Mengisi ulang password")
            steps.append(Step(kind="text_pass", meta=meta))
        elif "isi password" in low:
            meta = make_meta(line, label="Mengisi password")
            steps.append(Step(kind="text_pass", meta=meta))

        for w in post_waits:
            steps.append(Step(kind="wait", seconds=float(w), meta=make_meta(line, silent=True)))

    return steps, has_loop


def create_mailtm_account_with_module(
    *,
    mailtm_module: Any,
    password: str,
    otp_timeout: int,
    otp_poll: float,
    otp_regex: str,
) -> Tuple[str, OtpFuture]:
    attempt = 0
    while True:
        attempt += 1
        try:
            domains = mailtm_module.get_available_domains()
            if not domains:
                raise RuntimeError("No mail.tm domains available.")
            _acct_json, email, _pw = mailtm_module.create_random_mailtm_account(domains, password)
            if not email:
                raise RuntimeError("Failed to create mail.tm account.")
            token = mailtm_module.login_mailtm(email, password)
            if not token:
                raise RuntimeError("Failed to get mail.tm token.")
            otp_fut = start_otp_prefetch(
                mailtm_module=mailtm_module,
                token=token,
                timeout_s=otp_timeout,
                poll_s=otp_poll,
                otp_regex=otp_regex,
            )
            return email, otp_fut
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(
                f"mail.tm module attempt {attempt} failed: {exc}. Retry in {MAILTM_RETRY_DELAY_S:.0f}s...",
                file=sys.stderr,
            )
            sleep_interruptible(MAILTM_RETRY_DELAY_S)


def create_mailtm_account_with_client(*, base_url: str, password: str) -> Tuple[MailTm, str]:
    attempt = 0
    while True:
        attempt += 1
        mail = MailTm(base_url)
        try:
            domains = mail.domains()
            email = gen_address(domains)
            mail.create_account(email, password)
            mail.get_token(email, password)
            return mail, email
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(
                f"mail.tm client attempt {attempt} failed: {exc}. Retry in {MAILTM_RETRY_DELAY_S:.0f}s...",
                file=sys.stderr,
            )
            sleep_interruptible(MAILTM_RETRY_DELAY_S)


def main(argv: Optional[List[str]] = None) -> int:
    install_signal_handlers()
    ap = argparse.ArgumentParser(description="Flow khusus kordinat2.txt (automatic OTP gate setelah captcha).")
    ap.add_argument("--serial", help="ADB serial (default: auto jika hanya 1 device).")
    ap.add_argument("--adb-path", default="adb", help="adb path (default: adb)")
    ap.add_argument("--flow", default="kordinat2.txt", help="Path ke file kordinat2.txt")
    ap.add_argument("--count", type=int, default=5, help="Berapa akun yang ingin dijalankan.")
    ap.add_argument(
        "--otp-regex",
        default=r"\b(\d{6})\b",
        help="Regex (dengan capture group) untuk OTP. Default: 6 digit.",
    )
    ap.add_argument("--otp-timeout", type=int, default=180, help="OTP wait timeout seconds.")
    ap.add_argument("--otp-poll", type=float, default=2.0, help="OTP poll interval seconds.")
    ap.add_argument("--tap-delay", type=float, default=0.0, help="Delay setelah tiap tap (detik).")
    ap.add_argument("--text-delay", type=float, default=0.2, help="Delay setelah input teks (detik).")
    ap.add_argument(
        "--implicit-delay",
        type=float,
        default=1.0,
        help='Ekstra delay untuk tap tanpa instruksi waktu di kordinat (detik).',
    )
    ap.add_argument(
        "--prefer-mailtm-module",
        action="store_true",
        help="Gunakan mailtm.py lokal bila ada (lebih cepat).",
    )
    ap.add_argument(
        "--enter-timeout",
        type=float,
        default=0.0,
        help="Auto-continue setelah sekian detik untuk step 'wait enter' (0 = manual).",
    )
    ap.add_argument("--scroll-count", type=int, default=5, help="Jumlah swipe untuk step 'scroll'.")
    ap.add_argument("--scroll-duration-ms", type=int, default=450, help="Durasi swipe scroll (ms).")
    ap.add_argument("--scroll-pause", type=float, default=0.35, help="Pause antar scroll (detik).")
    ap.add_argument(
        "--no-enter-next",
        dest="enter_next",
        action="store_false",
        help="Jangan pause Enter sebelum akun berikutnya.",
    )
    ap.set_defaults(enter_next=True)
    ap.add_argument("--quiet", action="store_true", help="Kurangi logging.")
    ap.add_argument("--dry-run", action="store_true", help="Print aksi tanpa eksekusi adb/mail.")
    ap.add_argument("--mailtm-base", default=MAILTM_BASE_URL, help="mail.tm base url.")
    ap.add_argument("--save", default="created_accounts.jsonl", help="Simpan akun ke file jsonl ini.")
    args = ap.parse_args(argv)

    adb_path = prompt_adb_path(args.adb_path)
    serial = pick_serial(adb_path, args.serial)

    referral = input("Masukan kode reff (juga jadi password): ").strip()
    if not referral:
        print("Kode reff kosong.", file=sys.stderr)
        return 2
    password = referral

    steps, has_loop = parse_kordinat2_file(args.flow)
    if not steps:
        print(f"Tidak ada step kebaca dari {args.flow}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Device serial: {serial}")
        print(f"Flow steps: {len(steps)}")

    mailtm_mod = None
    if not args.dry_run:
        try:
            import mailtm as mailtm_mod  # type: ignore
        except Exception:
            mailtm_mod = None
        if args.prefer_mailtm_module and mailtm_mod is None:
            raise SystemExit(
                "Requested --prefer-mailtm-module but `mailtm.py` tidak bisa di-import (cek dependency requests?)."
            )

    try:
        for i in range(args.count):
            if STOP_REQUESTED:
                raise KeyboardInterrupt

            if i > 0 and not has_loop:
                wait_enter("Flow tidak ada 'back to no 1'. Siapkan ke posisi awal manual, lalu Enter...")

            mail_client: Optional[MailTm] = None
            if args.dry_run:
                email = "u123@example.com"
                otp_fut = None
            else:
                if mailtm_mod is not None:
                    email, otp_fut = create_mailtm_account_with_module(
                        mailtm_module=mailtm_mod,
                        password=password,
                        otp_timeout=args.otp_timeout,
                        otp_poll=args.otp_poll,
                        otp_regex=args.otp_regex,
                    )
                else:
                    mail_client, email = create_mailtm_account_with_client(
                        base_url=args.mailtm_base,
                        password=password,
                    )
                    otp_fut = None

            print(f"[{i+1}/{args.count}] Membuat akun dengan email: {email}")

            otp_seed = "123456" if args.dry_run else ""

            run_steps(
                adb_path=adb_path,
                serial=serial,
                steps=steps,
                email=email,
                otp=otp_seed,
                referral=referral,
                password=password,
                dry_run=args.dry_run,
                mail=None if args.dry_run else mail_client,
                otp_regex=args.otp_regex,
                otp_timeout_s=args.otp_timeout,
                otp_poll_s=args.otp_poll,
                after_tap_delay_s=float(args.tap_delay),
                after_text_delay_s=float(args.text_delay),
                implicit_delay_s=float(args.implicit_delay),
                otp_future=otp_fut,
                enter_timeout_s=float(args.enter_timeout),
                verbose=not bool(args.quiet),
                scroll_count=int(args.scroll_count),
                scroll_duration_ms=int(args.scroll_duration_ms),
                scroll_pause_s=float(args.scroll_pause),
            )

            rec = {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "serial": serial,
                "email": email,
                "password": password,
            }
            try:
                if not args.dry_run:
                    with open(args.save, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"Warning: gagal save {args.save}: {e}", file=sys.stderr)

            if i < args.count - 1 and bool(args.enter_next):
                wait_enter("Selesai 1 akun. Siapkan ke posisi awal, lalu Enter buat lanjut akun berikutnya...")
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
