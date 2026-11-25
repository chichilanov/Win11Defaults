# -*- coding: utf-8 -*-
"""
Win11 Defaults Manager 2.2
Возвращает в Windows 11 функцию "По умолчанию" для приложения + управление UCPD.sys.
Поддерживает пакетный режим: --apply/--export JSON-профилей.
profile.json ищется рядом с exe, если путь не указан явно.

Только стандартная библиотека. Сборка:
  pyinstaller -F -w --uac-admin --add-data "SetUserFTA.exe;." --name Win11Defaults win11_defaults.py
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import winreg
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# ----------------------------------------------------------------- константы
APP_TITLE = "Значения по умолчанию — Windows 11"
PROFILE_VERSION = 1
HKLM, HKCU, HKCR = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER,
                    winreg.HKEY_CLASSES_ROOT)

FILEEXTS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
URLASSOC = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
UCPD_KEY = r"SYSTEM\CurrentControlSet\Services\UCPD"
UCPD_TASK_PATH = r"\Microsoft\Windows\AppxDeploymentClient\\"
UCPD_TASK_NAME = "UCPD velocity"
POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows\System"
POLICY_VAL = "DefaultAssociationsConfiguration"

CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "Win11DefaultsManager")
CFG_FILE = os.path.join(CFG_DIR, "config.json")
POLICY_DIR = r"C:\ProgramData\Win11DefaultsManager"
POLICY_XML = os.path.join(POLICY_DIR, "DefaultAssociations.xml")

NOWIN = 0x08000000  # CREATE_NO_WINDOW
SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = os.path.join(SYSROOT, "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")

START_NAMES = {0: "Boot (ядро)", 1: "System (система)", 2: "Авто",
               3: "Вручную (по умолчанию)", 4: "Отключён"}


# ----------------------------------------------------------------- утилиты
def run(args, timeout=90):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NOWIN)
    except Exception as e:
        return -1, str(e)
    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("oem", "utf-8", "cp1251"):
        try:
            return p.returncode, raw.decode(enc, "replace").strip()
        except Exception:
            continue
    return p.returncode, repr(raw)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    params = '"%s"' % os.path.abspath(sys.argv[0])
    if len(sys.argv) > 1:
        params += " " + " ".join('"%s"' % a for a in sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params,
                                        None, 1)


def load_cfg():
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------- реестр
def ropen(root, path, write=False):
    if not path:
        return None
    acc = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    if root in (HKLM, HKCR):
        acc |= winreg.KEY_WOW64_64KEY
    try:
        return winreg.OpenKey(root, path, 0, acc)
    except OSError:
        return None


def rget(key, name=""):
    if key is None:
        return None
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def rvals(key):
    out, i = {}, 0
    if key is None:
        return out
    while True:
        try:
            n, v, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        out[n] = v
        i += 1
    return out


def rsubs(key):
    out, i = [], 0
    if key is None:
        return out
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            break
        i += 1
    return out


def resolve_string(s):
    if not isinstance(s, str) or not s:
        return ""
    s = os.path.expandvars(s)
    if s.startswith("@"):
        buf = ctypes.create_unicode_buffer(2048)
        try:
            if ctypes.windll.shlwapi.SHLoadIndirectString(s, buf, 2048, None) == 0:
                return buf.value or s
        except Exception:
            pass
    return s


def exe_from_command(cmd):
    if not isinstance(cmd, str) or not cmd.strip():
        return ""
    cmd = os.path.expandvars(cmd.strip())
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end]
    low = cmd.lower()
    i = low.find(".exe")
    if i > 0:
        return cmd[:i + 4]
    return cmd.split()[0]


def file_description(path):
    if not path or not os.path.isfile(path):
        return ""
    try:
        v = ctypes.windll.version
        size = v.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not v.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, ln = ctypes.c_void_p(), ctypes.c_uint()
        langs = []
        if v.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                            ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
            arr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16))
            for i in range(ln.value // 4):
                langs.append("%04x%04x" % (arr[i * 2], arr[i * 2 + 1]))
        langs += ["040904b0", "040904e4", "041904b0"]
        for lang in langs:
            if v.VerQueryValueW(buf, "\\StringFileInfo\\%s\\FileDescription" % lang,
                                ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
                return ctypes.wstring_at(ptr.value, ln.value).strip("\x00")
    except Exception:
        pass
    return ""


def find_exe_by_name(exename):
    for root in (HKLM, HKCU):
        k = ropen(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\"
                        + exename)
        p = rget(k)
        if isinstance(p, str) and os.path.isfile(os.path.expandvars(p.strip('"'))):
            return os.path.expandvars(p.strip('"'))
    k = ropen(HKCR, "Applications\\" + exename + r"\shell\open\command")
    p = exe_from_command(rget(k))
    if p and os.path.isfile(p):
        return p
    return shutil.which(exename) or ""


def progid_command(progid):
    if not progid:
        return ""
    k = ropen(HKCR, progid + r"\shell\open\command")
    cmd = rget(k)
    if isinstance(cmd, str) and cmd:
        return cmd
    if progid.lower().startswith("applications\\"):
        return find_exe_by_name(progid.split("\\", 1)[1])
    return ""


def progid_exists(progid):
    return bool(progid) and ropen(HKCR, progid) is not None


def progid_title(progid):
    """Человекочитаемое имя приложения для ProgID."""
    if not progid:
        return "(не задано)"
    k = ropen(HKCR, progid)
    ftn = rget(k, "FriendlyTypeName")
    if isinstance(ftn, str) and ftn:
        resolved = resolve_string(ftn)
        low = resolved.lower()
        if not any(s in low for s in (" file", " format", " document",
                                       " sheet", " archive", " codec",
                                       " folder", " shortcut", " image",
                                       " audio", " video", " playlist")):
            return resolved
    cmd = progid_command(progid)
    if cmd:
        exe = exe_from_command(cmd)
        if not exe and progid.lower().startswith("applications\\"):
            exe = find_exe_by_name(progid.split("\\", 1)[1])
        if exe and os.path.isfile(exe):
            desc = file_description(exe)
            if desc:
                return desc
            return os.path.basename(exe)
    if progid.lower().startswith("applications\\"):
        k2 = ropen(HKCR, progid)
        name = rget(k2)
        if isinstance(name, str) and name:
            return resolve_string(name)
    return progid


def current_progid(ident):
    """Эффективный ProgID: UserChoice → HKCR\.ext → OpenWithProgids."""
    if ident.startswith("."):
        path = FILEEXTS + "\\" + ident + r"\UserChoice"
    else:
        path = URLASSOC + "\\" + ident + r"\UserChoice"
    pid = rget(ropen(HKCU, path), "ProgId")
    if pid:
        return pid
    if ident.startswith("."):
        val = rget(ropen(HKCR, ident))
        if isinstance(val, str) and val and progid_exists(val):
            return val
        opk = ropen(HKCR, ident + r"\OpenWithProgids")
        for name in rvals(opk):
            if name and progid_exists(name):
                cmd = progid_command(name)
                if cmd:
                    return name
    return ""


# ----------------------------------------------------------------- модель
@dataclass
class AppInfo:
    name: str
    exe: str = ""
    source: str = ""
    exts: dict = field(default_factory=dict)
    protos: dict = field(default_factory=dict)


def collect_apps():
    apps = {}

    def add(name, exe, exts, protos, source):
        name = (name or "").strip() or "Без имени"
        key = (name.lower(), os.path.basename(exe or "").lower())
        a = apps.get(key)
        if a is None:
            a = AppInfo(name=name, exe=exe or "", source=source)
            apps[key] = a
        if exe and not a.exe:
            a.exe = exe
        for e, p in exts.items():
            if e.startswith(".") and isinstance(p, str) and p:
                a.exts.setdefault(e.lower(), p)
        for pr, p in protos.items():
            if isinstance(p, str) and p:
                a.protos.setdefault(pr.lower(), p)

    for root, node in ((HKLM, r"SOFTWARE\RegisteredApplications"),
                       (HKLM, r"SOFTWARE\WOW6432Node\RegisteredApplications"),
                       (HKCU, r"SOFTWARE\RegisteredApplications")):
        for regname, cappath in rvals(ropen(root, node)).items():
            if not isinstance(cappath, str):
                continue
            cap = ropen(root, cappath)
            if cap is None:
                continue
            name = resolve_string(rget(cap, "ApplicationName")) or regname
            exts = {k.lower(): v for k, v in
                    rvals(ropen(root, cappath + r"\FileAssociations")).items()}
            protos = {k.lower(): v for k, v in
                      rvals(ropen(root, cappath + r"\URLAssociations")).items()}
            exe = ""
            for pid in list(exts.values()) + list(protos.values()):
                exe = exe_from_command(progid_command(pid))
                if exe:
                    break
            if not exe and regname.lower().endswith(".exe"):
                exe = find_exe_by_name(regname)
            add(name, exe, exts, protos, "RegisteredApplications")

    for root, base in ((HKLM, r"SOFTWARE\Classes\Applications"),
                       (HKCU, r"Software\Classes\Applications")):
        bk = ropen(root, base)
        for exename in rsubs(bk):
            if not exename.lower().endswith(".exe"):
                continue
            sub = base + "\\" + exename
            st = ropen(root, sub + r"\SupportedTypes")
            exts = {e.lower(): "Applications\\" + exename
                    for e in rvals(st) if e.startswith(".")}
            if not exts:
                continue
            exe = exe_from_command(rget(ropen(root, sub + r"\shell\open\command")))
            if not exe or not os.path.isfile(exe):
                exe = find_exe_by_name(exename)
            name = (resolve_string(rget(ropen(root, sub))) or
                    file_description(exe) or exename)
            add(name, exe, exts, {}, "Applications")

    ow_pid, ow_exe = {}, {}
    for root, base in ((HKCU, FILEEXTS), (HKLM, r"SOFTWARE\Classes"),
                       (HKCU, r"Software\Classes")):
        bk = ropen(root, base)
        for ext in rsubs(bk):
            if not ext.startswith("."):
                continue
            e = ext.lower()
            for subname, target in (("OpenWithProgids", ow_pid),
                                    ("OpenWithList", ow_exe)):
                sk = ropen(root, base + "\\" + ext + "\\" + subname)
                for n in rvals(sk):
                    if n:
                        target.setdefault(n.lower(), set()).add(e)

    for a in apps.values():
        own = {p.lower(): p for p in list(a.exts.values()) + list(a.protos.values())}
        base_exe = os.path.basename(a.exe).lower()
        cand = set(ow_exe.get(base_exe, set()))
        for pl in own:
            cand |= ow_pid.get(pl, set())
        appkey = "Applications\\" + base_exe if base_exe else ""
        has_appkey = bool(appkey) and progid_exists(appkey)
        for e in cand:
            if e in a.exts:
                continue
            pid = next((own[p] for p in ow_pid if p in own and
                        e in ow_pid[p]), "")
            if not pid and has_appkey:
                pid = appkey
            if pid:
                a.exts[e] = pid

    merged = {}
    for a in apps.values():
        if not a.exts and not a.protos:
            continue
        k = a.name.lower()
        if k in merged:
            m = merged[k]
            m.exe = m.exe or a.exe
            for e, p in a.exts.items():
                m.exts.setdefault(e, p)
            for e, p in a.protos.items():
                m.protos.setdefault(e, p)
        else:
            merged[k] = a
    return sorted(merged.values(), key=lambda x: x.name.lower())


# ----------------------------------------------------------------- UCPD
def ucpd_info():
    k = ropen(HKLM, UCPD_KEY)
    start = rget(k, "Start")
    _, out = run(["sc", "query", "ucpd"])
    running = "RUNNING" in out.upper()
    present = os.path.isfile(os.path.join(SYSROOT, "System32", "drivers", "UCPD.sys"))
    _, ts = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-ScheduledTask -TaskPath '%s' -TaskName '%s' "
                 "-ErrorAction SilentlyContinue).State" % (UCPD_TASK_PATH, UCPD_TASK_NAME)])
    return {"start": start, "running": running, "present": present,
            "task": ts.strip() or "не найдена"}


def ucpd_set(enable: bool, cfg, log):
    if not is_admin():
        log("ОШИБКА: нужны права администратора.")
        return False
    k = ropen(HKLM, UCPD_KEY, write=True)
    if k is None:
        log("Ключ UCPD не найден — драйвер отсутствует в этой сборке.")
        return False

    old = rget(k, "Start")
    if not enable and cfg.get("ucpd_original_start") is None:
        cfg["ucpd_original_start"] = old if isinstance(old, int) else 3
        save_cfg(cfg)

    os.makedirs(CFG_DIR, exist_ok=True)
    backup = os.path.join(CFG_DIR, "ucpd_backup_%s.reg" % time.strftime("%Y%m%d_%H%M%S"))
    run(["reg", "export", "HKLM\\" + UCPD_KEY, backup, "/y"])
    log("Резервная копия ветки реестра: " + backup)

    target = (cfg.get("ucpd_original_start", 3) if enable else 4)
    try:
        winreg.SetValueEx(k, "Start", 0, winreg.REG_DWORD, int(target))
        log("Services\\UCPD -> Start = %s (%s)" % (target, START_NAMES.get(target, "?")))
    except OSError as e:
        log("ОШИБКА записи Start: %s" % e)
        return False

    verb = "Enable-ScheduledTask" if enable else "Disable-ScheduledTask"
    rc, out = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                   "%s -TaskPath '%s' -TaskName '%s' -ErrorAction Stop" %
                   (verb, UCPD_TASK_PATH, UCPD_TASK_NAME)])
    if rc != 0:
        rc2, out2 = run(["schtasks", "/Change", "/TN",
                         UCPD_TASK_PATH + UCPD_TASK_NAME,
                         "/Enable" if enable else "/Disable"])
        out = out + "\n" + out2
        rc = rc2
    log("Задача 'UCPD velocity': " + ("OK" if rc == 0 else "НЕ УДАЛОСЬ (%s)" % out))
    if rc != 0:
        log("Подсказка: задача защищена. Отключите её из-под SYSTEM "
            "(PsExec -s -i powershell) или запретите SYSTEM запись в ключ UCPD.")
    return True


# ----------------------------------------------------------------- SetUserFTA
def find_sufta(cfg):
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "SetUserFTA.exe")
        if os.path.isfile(p):
            return p
    for d in (os.path.dirname(os.path.abspath(sys.argv[0])), os.getcwd()):
        p = os.path.join(d, "SetUserFTA.exe")
        if os.path.isfile(p):
            return p
    p = cfg.get("sufta", "")
    if p and os.path.isfile(p):
        return p
    p = shutil.which("SetUserFTA.exe")
    return p or ""


# ----------------------------------------------------------------- движки
def apply_policy(items, log):
    if not is_admin():
        log("ОШИБКА: для политики нужны права администратора.")
        return 0, len(items)
    os.makedirs(POLICY_DIR, exist_ok=True)
    table = {}
    if os.path.isfile(POLICY_XML):
        try:
            for a in ET.parse(POLICY_XML).getroot().findall("Association"):
                table[a.get("Identifier")] = (a.get("ProgId"),
                                              a.get("ApplicationName", ""))
            log("Существующая конфигурация объединена (%d записей)." % len(table))
        except Exception as e:
            log("Не удалось разобрать старый XML: %s" % e)
    for ident, pid, appname in items:
        table[ident] = (pid, appname)

    root = ET.Element("DefaultAssociations")
    for ident in sorted(table):
        pid, appname = table[ident]
        ET.SubElement(root, "Association", Identifier=ident, ProgId=pid,
                      ApplicationName=appname)
    ET.ElementTree(root).write(POLICY_XML, encoding="utf-8", xml_declaration=True)
    log("XML записан: %s (%d записей)" % (POLICY_XML, len(table)))

    k = ropen(HKLM, POLICY_KEY, write=True)
    if k is None:
        try:
            k = winreg.CreateKeyEx(HKLM, POLICY_KEY, 0,
                                   winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY)
        except OSError as e:
            log("ОШИБКА создания ключа политики: %s" % e)
            return 0, len(items)
    winreg.SetValueEx(k, POLICY_VAL, 0, winreg.REG_SZ, POLICY_XML)
    log("Политика включена. Применится при следующем входе в систему.")
    return len(items), 0


def policy_disable(log):
    if not is_admin():
        log("ОШИБКА: нужны права администратора.")
        return
    k = ropen(HKLM, POLICY_KEY, write=True)
    if k is None:
        log("Политика не задана.")
        return
    try:
        winreg.DeleteValue(k, POLICY_VAL)
        log("Политика DefaultAssociationsConfiguration снята. Файл XML: " + POLICY_XML)
    except OSError as e:
        log("Не удалось снять политику: %s" % e)


def apply_sufta(items, cfg, log, progress_cb=None):
    exe = find_sufta(cfg)
    if not exe:
        log("SetUserFTA.exe не найден. Положите его рядом со скриптом/exe "
            "или укажите путь в настройках.")
        return 0, len(items)
    info = ucpd_info()
    if info["running"] or info["start"] not in (4, None):
        log("ВНИМАНИЕ: UCPD активен (Start=%s, running=%s). Запись UserChoice "
            "может блокироваться — отключите UCPD и перезагрузитесь."
            % (info["start"], info["running"]))
    ok = err = 0
    for i, (ident, pid, _name) in enumerate(items, 1):
        if not progid_exists(pid):
            log("пропуск %s: ProgID '%s' не зарегистрирован" % (ident, pid))
            err += 1
        else:
            rc, out = run([exe, ident, pid])
            real = current_progid(ident)
            if rc == 0 and real.lower() == pid.lower():
                ok += 1
                log("%s -> %s  OK" % (ident, pid))
            else:
                err += 1
                log("%s -> %s  ОШИБКА (code=%s, факт=%s) %s"
                    % (ident, pid, rc, real or "пусто", out))
        if progress_cb:
            progress_cb(i, len(items))
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:
        pass
    return ok, err


# ----------------------------------------------------------------- профили JSON
def export_profile(path, log):
    apps = {}
    seen = set()

    def add(ident, pid):
        if not pid:
            return
        if (ident, pid) in seen:
            return
        seen.add((ident, pid))
        exe = exe_from_command(progid_command(pid))
        name = (progid_title(pid) or os.path.basename(exe) or pid).strip()
        if not name:
            name = pid
        a = apps.setdefault(name, {"exe": exe, "exts": {}, "protos": {}})
        if not a["exe"] and exe:
            a["exe"] = exe
        if ident.startswith("."):
            a["exts"][ident] = pid
        else:
            a["protos"][ident] = pid

    for ext in rsubs(ropen(HKCU, FILEEXTS)):
        if not ext.startswith("."):
            continue
        pid = current_progid(ext)
        add(ext, pid)

    for proto in rsubs(ropen(HKCU, URLASSOC)):
        pid = current_progid(proto)
        add(proto, pid)

    ucpd = ucpd_info()
    pol = rget(ropen(HKLM, POLICY_KEY), POLICY_VAL) or ""

    profile = {
        "version": PROFILE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": os.environ.get("USERNAME", ""),
        "computer": os.environ.get("COMPUTERNAME", ""),
        "apps": apps,
        "ucpd": {
            "start": ucpd["start"],
            "running": ucpd["running"],
            "task": ucpd["task"],
        },
        "policy_xml_path": pol,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    total = sum(len(a["exts"]) + len(a["protos"]) for a in apps.values())
    log("Профиль экспортирован: %s (%d приложений, %d ассоциаций)" %
        (path, len(apps), total))
    return profile


def load_profile(path, log):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != PROFILE_VERSION:
        log("ВНИМАНИЕ: версия профиля %s, ожидается %s" %
            (data.get("version"), PROFILE_VERSION))
    apps = data.get("apps", {})
    items = []
    for app_name, a in apps.items():
        for ext, pid in a.get("exts", {}).items():
            items.append((ext, pid, app_name))
        for proto, pid in a.get("protos", {}).items():
            items.append((proto, pid, app_name))
    log("Профиль загружен: %s (%d приложений, %d ассоциаций)" %
        (path, len(apps), len(items)))
    return items


def apply_profile(path, engine, cfg, log, progress_cb=None):
    items = load_profile(path, log)
    if not items:
        log("Профиль пуст.")
        return 0, 0
    if engine == "policy":
        return apply_policy(items, log)
    return apply_sufta(items, cfg, log, progress_cb)


# ----------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="Win11Defaults",
        description="Управление ассоциациями файлов и драйвером UCPD в Windows 11.")
    p.add_argument("--export", metavar="FILE",
                   help="Экспортировать текущие ассоциации пользователя в JSON-профиль")
    p.add_argument("--apply", metavar="FILE", nargs="?", const="__default__",
                   help="Применить JSON-профиль (по умолчанию — profile.json рядом с exe)")
    p.add_argument("--engine", choices=["sufta", "policy"], default="sufta",
                   help="Движок: sufta (SetUserFTA) или policy (политика Microsoft)")
    p.add_argument("--disable-ucpd", action="store_true",
                   help="Отключить UCPD.sys (нужны права администратора)")
    p.add_argument("--enable-ucpd", action="store_true",
                   help="Восстановить UCPD.sys")
    p.add_argument("--disable-policy", action="store_true",
                   help="Снять политику DefaultAssociationsConfiguration")
    p.add_argument("--reboot", action="store_true",
                   help="Перезагрузить ПК после выполнения команд")
    p.add_argument("--sufta", metavar="PATH",
                   help="Путь к SetUserFTA.exe (сохраняется в конфиг)")
    return p


def cli_main(args):
    cfg = load_cfg()
    if args.sufta:
        cfg["sufta"] = os.path.abspath(args.sufta)
        save_cfg(cfg)
        print("Путь к SetUserFTA сохранён:", cfg["sufta"])

    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))

    def log(msg):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    if args.disable_ucpd:
        ucpd_set(False, cfg, log)
    if args.enable_ucpd:
        ucpd_set(True, cfg, log)
    if args.disable_policy:
        policy_disable(log)

    if args.export:
        path = os.path.abspath(args.export)
        export_profile(path, log)
    elif args.apply:
        if args.apply == "__default__":
            path = os.path.join(base_dir, "profile.json")
        else:
            path = os.path.abspath(args.apply)
        if not os.path.isfile(path):
            print("Файл профиля не найден: %s" % path)
            return
        ok, err = apply_profile(path, args.engine, cfg, log)
        log("Готово: успешно %d, ошибок %d" % (ok, err))
    else:
        default_profile = os.path.join(base_dir, "profile.json")
        if os.path.isfile(default_profile):
            log("Файл не указан, применяем %s" % default_profile)
            ok, err = apply_profile(default_profile, args.engine, cfg, log)
            log("Готово: успешно %d, ошибок %d" % (ok, err))
        else:
            print("profile.json не найден рядом с программой.")
            print("Использование: Win11Defaults.exe [--apply [FILE]] [--engine sufta|policy] ...")

    if args.reboot:
        print("Перезагрузка через 5 секунд...")
        run(["shutdown", "/r", "/t", "5", "/c", "Win11Defaults: завершение операции"])


# ----------------------------------------------------------------- GUI
HELP_TEXT = """КАК ПОЛЬЗОВАТЬСЯ

1. Вкладка "UCPD.sys" -> "Отключить UCPD" -> перезагрузка.
   "Восстановить UCPD" возвращает исходное значение Start и включает задачу.
   Если задача 'UCPD velocity' защищена и не отключается, сделайте это из-под
   SYSTEM:  psexec -s -i powershell  ->  Disable-ScheduledTask -TaskPath
   '\\Microsoft\\Windows\\AppxDeploymentClient\\' -TaskName 'UCPD velocity'

2. Вкладка "Приложения": слева выберите программу — справа появятся все её
   расширения и протоколы (источники: RegisteredApplications\\Capabilities,
   Applications\\*.exe\\SupportedTypes, OpenWithProgids/OpenWithList).
   Зелёным отмечено то, где программа уже стоит по умолчанию.

3. Нажмите большую синюю кнопку "По умолчанию" — программе будут назначены
   сразу ВСЕ её расширения и протоколы (это и есть недостающая функция Win11).
   Либо отметьте галочками нужные строки (клик по квадратику или Пробел) и
   нажмите "Применить отмеченные".

4. Профили: "Экспорт профиля" сохраняет текущие ассоциации пользователя в JSON.
   "Импорт профиля" применяет JSON-профиль. Удобно для развёртывания на
   нескольких ПК: один раз настроили эталон, выгрузили профиль, применили
   на остальных.

ДВИЖКИ

№1 SetUserFTA — мгновенно, по одному расширению.
   Windows 10/11 хранит выбор в HKCU\\...\\FileExts\\.ext\\UserChoice со значением
   Hash (SID + расширение + ProgID + время записи ключа). Без корректного Hash
   Explorer сбрасывает ассоциацию, поэтому просто написать ProgId недостаточно.
   Hash считает утилита SetUserFTA (kolbi.cz). В собранном exe SetUserFTA.exe
   встраивается внутрь и распаковывается автоматически. На свежих сборках
   запись в UserChoice блокирует драйвер UCPD — отсюда шаг 1.

№2 Политика DefaultAssociationsConfiguration — официальный путь Microsoft,
   работает без сторонних файлов и без отключения UCPD. Программа создаёт
   C:\\ProgramData\\Win11DefaultsManager\\DefaultAssociations.xml (объединяя с уже
   существующим) и прописывает путь в политику. Ассоциации применяются при
   входе в систему и переприменяются при каждом входе — ручной выбор
   пользователя будет перекрываться. "Снять политику ассоциаций" отключает это.

ПАКЕТНЫЙ РЕЖИМ (CLI)

  Win11Defaults.exe --export profile.json
  Win11Defaults.exe --apply profile.json
  Win11Defaults.exe --apply profile.json --engine policy
  Win11Defaults.exe --disable-ucpd --apply profile.json --reboot
  Win11Defaults.exe                       # применит profile.json из папки программы

ВАЖНО

- Запускайте программу под ТОЙ ЖЕ учётной записью, которой меняете ассоциации:
  значения пишутся в HKCU. Если повысить права через UAC под другим
  админ-аккаунтом, настройки уйдут в другой профиль.
- Отключение UCPD снимает защитный механизм Microsoft; делайте это осознанно.
  Обновления Windows могут вернуть драйвер — проверяйте статус после апдейтов.
- Протоколы http/https/mailto меняйте только для браузеров/почтовых клиентов.
- Резервные копии ветки UCPD сохраняются в %APPDATA%\\Win11DefaultsManager.
"""


class MainWindow:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
        from tkinter.scrolledtext import ScrolledText
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.filedialog = filedialog
        self.ScrolledText = ScrolledText

        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.apps, self.filtered, self.cur = [], [], None
        self.rows = {}
        self.busy = False

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1200x780")
        self.root.minsize(1000, 640)
        self._styles()
        self._build()
        self.root.after(80, self._poll)
        self.refresh_apps()
        self.refresh_ucpd()
        self.root.mainloop()

    def _styles(self):
        s = self.ttk.Style(self.root)
        try:
            s.theme_use("vista")
        except Exception:
            pass
        base = ("Segoe UI", 10)
        self.root.configure(bg="#f3f3f3")
        s.configure(".", font=base)
        s.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        s.configure("H2.TLabel", font=("Segoe UI Semibold", 12))
        s.configure("Sub.TLabel", foreground="#606060")
        s.configure("Treeview", rowheight=24, font=base)
        s.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def _build(self):
        tk, ttk = self.tk, self.ttk
        hdr = ttk.Frame(self.root, padding=(16, 12, 16, 6))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Приложения по умолчанию", style="Title.TLabel").pack(anchor="w")
        wv = sys.getwindowsversion()
        info = "Windows build %d.%d - %s - пользователь: %s" % (
            wv.major, wv.build,
            "администратор" if is_admin() else "обычные права",
            os.environ.get("USERNAME", "?"))
        ttk.Label(hdr, text=info, style="Sub.TLabel").pack(anchor="w")
        if not is_admin():
            ttk.Button(hdr, text="Перезапустить от имени администратора",
                       command=self._relaunch).pack(anchor="w", pady=(6, 0))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=8)
        self._tab_apps()
        self._tab_ucpd()
        self._tab_log()
        self._tab_help()

    def _tab_apps(self):
        tk, ttk = self.tk, self.ttk
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="  Приложения  ")
        pan = ttk.Panedwindow(tab, orient="horizontal")
        pan.pack(fill="both", expand=True)

        left = ttk.Frame(pan, padding=(0, 0, 8, 0))
        ttk.Label(left, text="Установленные приложения", style="H2.TLabel").pack(anchor="w")
        self.var_search = tk.StringVar()
        e = ttk.Entry(left, textvariable=self.var_search)
        e.pack(fill="x", pady=6)
        self.var_search.trace_add("write", lambda *_: self._filter_apps())
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True)
        self.tv_apps = ttk.Treeview(lf, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tv_apps.yview)
        self.tv_apps.configure(yscrollcommand=sb.set)
        self.tv_apps.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv_apps.bind("<<TreeviewSelect>>", self._on_app_select)
        ttk.Button(left, text="Обновить список", command=self.refresh_apps).pack(fill="x", pady=6)
        pan.add(left, weight=1)

        right = ttk.Frame(pan, padding=(8, 0, 0, 0))
        pan.add(right, weight=3)
        self.lbl_app = ttk.Label(right, text="Выберите приложение слева", style="Title.TLabel")
        self.lbl_app.pack(anchor="w")
        self.lbl_path = ttk.Label(right, text="", style="Sub.TLabel", wraplength=760)
        self.lbl_path.pack(anchor="w", pady=(0, 8))

        eng = ttk.Frame(right)
        eng.pack(fill="x")
        ttk.Label(eng, text="Движок:").pack(side="left")
        self.var_engine = tk.IntVar(value=int(self.cfg.get("engine", 0)))
        self.cb_engine = ttk.Combobox(eng, state="readonly", width=58, values=[
            "SetUserFTA - мгновенно (нужен отключённый UCPD)",
            "Политика DefaultAssociations - без сторонних ПО (нужен повторный вход)"])
        self.cb_engine.current(self.var_engine.get())
        self.cb_engine.pack(side="left", padx=6)
        self.cb_engine.bind("<<ComboboxSelected>>", self._on_engine)
        ttk.Button(eng, text="Найти SetUserFTA.exe...", command=self._locate_sufta).pack(side="left")

        self.var_protos = tk.BooleanVar(value=bool(self.cfg.get("include_protocols", True)))
        ttk.Checkbutton(right, text="Включать протоколы (http, https, mailto...)",
                        variable=self.var_protos,
                        command=lambda: self._fill_rows(self.cur)).pack(anchor="w", pady=6)

        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(0, 8))
        self.btn_all = tk.Button(bar, text="По умолчанию", font=("Segoe UI Semibold", 11),
                                 bg="#0067c0", fg="white", activebackground="#005ba1",
                                 activeforeground="white", relief="flat", bd=0,
                                 padx=22, pady=8, cursor="hand2",
                                 command=lambda: self.apply(False))
        self.btn_all.pack(side="left")
        ttk.Button(bar, text="Применить отмеченные",
                   command=lambda: self.apply(True)).pack(side="left", padx=8)
        ttk.Button(bar, text="Отметить все", command=lambda: self._check_all(True)).pack(side="left")
        ttk.Button(bar, text="Снять все", command=lambda: self._check_all(False)).pack(side="left", padx=4)
        ttk.Button(bar, text="Параметры Windows",
                   command=lambda: os.startfile("ms-settings:defaultapps")).pack(side="right")

        prof = ttk.Frame(right)
        prof.pack(fill="x", pady=(0, 6))
        ttk.Button(prof, text="Экспорт профиля...", command=self._export_profile).pack(side="left")
        ttk.Button(prof, text="Импорт профиля...", command=self._import_profile).pack(side="left", padx=6)

        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True)
        cols = ("chk", "ext", "cur", "pid")
        self.tv = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for c, t, w, a in (("chk", "", 36, "center"), ("ext", "Расширение / протокол", 190, "w"),
                           ("cur", "Текущая программа", 260, "w"), ("pid", "ProgID (цель)", 280, "w")):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor=a, stretch=(c != "chk"))
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vs.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv.tag_configure("done", foreground="#0f7b0f")
        self.tv.tag_configure("proto", foreground="#5b5bd6")
        self.tv.bind("<Button-1>", self._on_row_click)
        self.tv.bind("<space>", lambda e: self._toggle_selected())

        bot = ttk.Frame(right)
        bot.pack(fill="x", pady=6)
        self.pb = ttk.Progressbar(bot, mode="determinate")
        self.pb.pack(fill="x")
        self.lbl_status = ttk.Label(bot, text="Готово", style="Sub.TLabel")
        self.lbl_status.pack(anchor="w", pady=(4, 0))

    def _tab_ucpd(self):
        ttk = self.ttk
        tab = ttk.Frame(self.nb, padding=16)
        self.nb.add(tab, text="  UCPD.sys  ")

        ttk.Label(tab, text="Управление защитным драйвером UCPD.sys",
                  style="Title.TLabel").pack(anchor="w")

        self.lbl_ucpd = ttk.Label(tab, text="...", justify="left", font=("Segoe UI", 10))
        self.lbl_ucpd.pack(anchor="w", pady=10)

        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Отключить UCPD",
                   command=lambda: self._ucpd(False)).pack(side="left")
        ttk.Button(row, text="Восстановить UCPD",
                   command=lambda: self._ucpd(True)).pack(side="left", padx=8)
        ttk.Button(row, text="Обновить статус",
                   command=self.refresh_ucpd).pack(side="left")
        ttk.Button(row, text="Перезагрузить ПК",
                   command=self._reboot).pack(side="left", padx=8)

        ttk.Separator(tab).pack(fill="x", pady=12)

        ttk.Label(tab, text="Политика ассоциаций (Движок №2)",
                  style="H2.TLabel").pack(anchor="w")
        self.lbl_policy = ttk.Label(tab, text="...", justify="left", font=("Segoe UI", 10))
        self.lbl_policy.pack(anchor="w", pady=6)

        r2 = ttk.Frame(tab)
        r2.pack(anchor="w", pady=6)
        ttk.Button(r2, text="Снять политику ассоциаций",
                   command=lambda: self._thread(lambda: policy_disable(self.log))).pack(side="left")
        ttk.Button(r2, text="Открыть XML",
                   command=self._open_xml).pack(side="left", padx=8)

        txt = (
            "═══════════════════════════════════════════════════════════\n"
            "ЧТО ТАКОЕ UCPD.SYS?\n"
            "═══════════════════════════════════════════════════════════\n\n"
            "UCPD (User Choice Protection Driver) — это защитный драйвер Microsoft,\n"
            "который не даёт программам менять ассоциации файлов без вашего ведома.\n\n"
            "Когда UCPD АКТИВЕН (по умолчанию):\n"
            "  - Защита включена — ассоциации защищены от подмены\n"
            "  - Программа НЕ МОЖЕТ изменить ассоциации через SetUserFTA\n"
            "  - Для работы Движка №1 (SetUserFTA) нужно отключить UCPD\n\n"
            "Когда UCPD ОТКЛЮЧЁН:\n"
            "  - Защита снята — можно менять ассоциации\n"
            "  - Движок №1 (SetUserFTA) работает мгновенно\n"
            "  - Требуется ПЕРЕЗАГРУЗКА после отключения\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "ЧТО ОЗНАЧАЮТ ЗНАЧЕНИЯ START?\n"
            "═══════════════════════════════════════════════════════════\n\n"
            "Start — это режим запуска драйвера (как в services.msc):\n\n"
            "  - Start: 0 (Boot)     — загружается при загрузке ядра Windows\n"
            "  - Start: 1 (System)   — загружается при загрузке системы\n"
            "  - Start: 2 (Auto)     — запускается автоматически\n"
            "  - Start: 3 (Manual)   — запускается вручную [ПО УМОЛЧАНИЮ для UCPD]\n"
            "  - Start: 4 (Disabled) — отключён [НУЖНО для работы SetUserFTA]\n\n"
            "Нормальное состояние: Start: 3 (Вручную), драйвер запущен.\n"
            "Для работы программы: Start: 4 (Отключён), драйвер не запущен.\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "КАК ПОЛЬЗОВАТЬСЯ?\n"
            "═══════════════════════════════════════════════════════════\n\n"
            "Сценарий 1: Настройка ассоциаций на своём ПК\n"
            "  1. Нажмите «Отключить UCPD» -> перезагрузите ПК\n"
            "  2. Перейдите на вкладку «Приложения» -> выберите программу\n"
            "  3. Нажмите «По умолчанию» — ассоциации применятся мгновенно\n"
            "  4. (Опционально) Нажмите «Восстановить UCPD» -> перезагрузите ПК\n"
            "     Защита вернётся, но ваши ассоциации останутся\n\n"
            "Сценарий 2: Развёртывание на нескольких ПК (пакетный режим)\n"
            "  Используйте CLI-команды:\n"
            "    Win11Defaults.exe --disable-ucpd --reboot\n"
            "    Win11Defaults.exe --engine sufta\n"
            "    Win11Defaults.exe --enable-ucpd --reboot\n\n"
            "Сценарий 3: Использование Движка №2 (Политика)\n"
            "  UCPD отключать НЕ нужно. Политика применяется через HKLM\n"
            "  и работает даже с активным UCPD. Но она перекрывает ручной\n"
            "  выбор пользователя при каждом входе в систему.\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "ВАЖНО\n"
            "═══════════════════════════════════════════════════════════\n\n"
            "- Отключение UCPD снимает защитный механизм Microsoft\n"
            "- Делайте это осознанно, только для настройки ассоциаций\n"
            "- После настройки рекомендуется восстановить UCPD\n"
            "- Обновления Windows могут вернуть UCPD — проверяйте статус\n"
            "- Резервные копии реестра сохраняются в %APPDATA%\\Win11DefaultsManager"
        )

        txt_frame = ttk.Frame(tab)
        txt_frame.pack(fill="both", expand=True, pady=12)

        txt_widget = self.ScrolledText(txt_frame, wrap="word", font=("Consolas", 9),
                                       height=18, state="disabled")
        txt_widget.pack(fill="both", expand=True)
        txt_widget.configure(state="normal")
        txt_widget.insert("1.0", txt)
        txt_widget.configure(state="disabled")

    def _tab_log(self):
        ttk = self.ttk
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="  Журнал  ")
        self.txt = self.ScrolledText(tab, wrap="word", font=("Consolas", 10), height=20)
        self.txt.pack(fill="both", expand=True)
        self.txt.configure(state="disabled")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Сохранить журнал...", command=self._save_log).pack(side="left")
        ttk.Button(row, text="Очистить", command=self._clear_log).pack(side="left", padx=6)

    def _tab_help(self):
        ttk = self.ttk
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="  Справка  ")
        t = self.ScrolledText(tab, wrap="word", font=("Segoe UI", 10))
        t.pack(fill="both", expand=True)
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")

    def log(self, msg):
        self.q.put(("log", "[%s] %s" % (time.strftime("%H:%M:%S"), msg)))

    def _thread(self, fn, *a):
        if self.busy:
            self.messagebox.showinfo("Занято", "Дождитесь завершения текущей операции.")
            return
        self.busy = True
        threading.Thread(target=self._wrap, args=(fn, a), daemon=True).start()

    def _wrap(self, fn, a):
        try:
            fn(*a)
        except Exception as e:
            self.log("ИСКЛЮЧЕНИЕ: %r" % e)
        finally:
            self.q.put(("busy", False))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", data + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "apps":
                    self.apps = data
                    self._filter_apps()
                    self.lbl_status.config(text="Найдено приложений: %d" % len(self.apps))
                elif kind == "progress":
                    i, n = data
                    self.pb.configure(maximum=max(n, 1), value=i)
                    self.lbl_status.config(text="Обработано %d из %d..." % (i, n))
                elif kind == "ucpd":
                    ucpd_text, ucpd_color, policy_text, policy_color = data
                    self.lbl_ucpd.config(text=ucpd_text, foreground=ucpd_color)
                    self.lbl_policy.config(text=policy_text, foreground=policy_color)
                elif kind == "rows":
                    self._fill_rows(self.cur)
                elif kind == "busy":
                    self.busy = bool(data)
                elif kind == "status":
                    self.lbl_status.config(text=data)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _relaunch(self):
        relaunch_as_admin()
        self.root.destroy()

    def _reboot(self):
        if self.messagebox.askyesno("Перезагрузка", "Перезагрузить компьютер сейчас?"):
            run(["shutdown", "/r", "/t", "5", "/c", "Win11 Defaults Manager: применение UCPD"])

    def _open_xml(self):
        if os.path.isfile(POLICY_XML):
            os.startfile(POLICY_XML)
        else:
            self.messagebox.showinfo("Нет файла", "XML ещё не создавался.")

    def _save_log(self):
        p = self.filedialog.asksaveasfilename(defaultextension=".txt",
                                              filetypes=[("Текст", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.txt.get("1.0", "end"))

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _on_engine(self, *_):
        self.var_engine.set(self.cb_engine.current())
        self.cfg["engine"] = self.cb_engine.current()
        save_cfg(self.cfg)

    def _locate_sufta(self):
        p = self.filedialog.askopenfilename(title="Укажите SetUserFTA.exe",
                                            filetypes=[("SetUserFTA", "SetUserFTA.exe"),
                                                       ("Все файлы", "*.*")])
        if p:
            self.cfg["sufta"] = p
            save_cfg(self.cfg)
            self.log("SetUserFTA: " + p)

    def _export_profile(self):
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        p = self.filedialog.asksaveasfilename(defaultextension=".json",
                                              filetypes=[("Профиль", "*.json")],
                                              initialfile="profile.json",
                                              initialdir=base_dir)
        if not p:
            return
        self._thread(lambda: export_profile(p, self.log))

    def _import_profile(self):
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        p = self.filedialog.askopenfilename(title="Выберите JSON-профиль",
                                            filetypes=[("Профиль", "*.json"),
                                                       ("Все файлы", "*.*")],
                                            initialdir=base_dir)
        if not p:
            return
        if not self.messagebox.askyesno("Импорт профиля",
                                        "Применить профиль %s?\nДвижок: %s" %
                                        (p, "SetUserFTA" if self.cb_engine.current() == 0
                                         else "политика")):
            return
        eng = "sufta" if self.cb_engine.current() == 0 else "policy"
        def job():
            ok, err = apply_profile(p, eng, self.cfg, self.log,
                                    lambda i, n: self.q.put(("progress", (i, n))))
            self.log("Готово: успешно %d, ошибок %d" % (ok, err))
            self.q.put(("status", "Готово: успешно %d, ошибок %d" % (ok, err)))
            self.q.put(("rows", None))
        self._thread(job)

    def refresh_apps(self):
        self.log("Сканирование реестра...")
        threading.Thread(target=lambda: self.q.put(("apps", collect_apps())),
                         daemon=True).start()

    def _filter_apps(self):
        q = self.var_search.get().strip().lower()
        self.filtered = [a for a in self.apps if q in a.name.lower()] if q else list(self.apps)
        self.tv_apps.delete(*self.tv_apps.get_children())
        for i, a in enumerate(self.filtered):
            n = len(a.exts) + (len(a.protos) if self.var_protos.get() else 0)
            self.tv_apps.insert("", "end", iid=str(i), text="%s  (%d)" % (a.name, n))

    def _on_app_select(self, *_):
        sel = self.tv_apps.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            if 0 <= idx < len(self.filtered):
                self.cur = self.filtered[idx]
                self._fill_rows(self.cur)
        except (ValueError, IndexError):
            pass

    def _fill_rows(self, a):
        self.tv.delete(*self.tv.get_children())
        self.rows.clear()
        if a is None:
            return
        self.lbl_app.config(text=a.name)
        self.lbl_path.config(text=(a.exe or "путь к exe не определён") +
                             "   -   источник: " + (a.source or "реестр"))
        items = [(e, p, False) for e, p in sorted(a.exts.items())]
        if self.var_protos.get():
            items += [(e, p, True) for e, p in sorted(a.protos.items())]
        for ident, pid, is_proto in items:
            cur = current_progid(ident)
            self.rows[ident] = {"pid": pid, "cur": cur, "proto": is_proto,
                                "checked": True,
                                "title": progid_title(cur)}
            self._draw_row(ident)
        done = sum(1 for r in self.rows.values() if r["cur"] == r["pid"])
        self.lbl_status.config(text="%s: %d элементов, уже по умолчанию - %d"
                               % (a.name, len(self.rows), done))
        self.pb.configure(value=0)

    def _draw_row(self, ident):
        r = self.rows[ident]
        tags = []
        if r["cur"] == r["pid"]:
            tags.append("done")
        elif r["proto"]:
            tags.append("proto")
        label = ident + ("  (протокол)" if r["proto"] else "")
        if not self.tv.exists(ident):
            self.tv.insert("", "end", iid=ident)
        self.tv.item(ident, values=("+" if r["checked"] else "-", label,
                                    r["title"], r["pid"]), tags=tags)

    def _on_row_click(self, e):
        if self.tv.identify_region(e.x, e.y) != "cell":
            return
        if self.tv.identify_column(e.x) != "#1":
            return
        iid = self.tv.identify_row(e.y)
        if iid in self.rows:
            self.rows[iid]["checked"] = not self.rows[iid]["checked"]
            self._draw_row(iid)

    def _toggle_selected(self):
        for iid in self.tv.selection():
            if iid in self.rows:
                self.rows[iid]["checked"] = not self.rows[iid]["checked"]
                self._draw_row(iid)

    def _check_all(self, state):
        for iid in self.rows:
            self.rows[iid]["checked"] = state
            self._draw_row(iid)

    def apply(self, only_checked):
        a = self.cur
        if a is None:
            self.messagebox.showinfo("Нет выбора", "Сначала выберите приложение.")
            return
        items, skipped = [], 0
        for ident, r in self.rows.items():
            if only_checked and not r["checked"]:
                continue
            if r["cur"] == r["pid"]:
                skipped += 1
                continue
            items.append((ident, r["pid"], a.name))
        if not items:
            self.messagebox.showinfo("Нечего менять",
                                     "Все выбранные элементы уже назначены этому приложению.")
            return
        eng = self.cb_engine.current()
        extra = ("\n\nПолитика применится после выхода и повторного входа в систему "
                 "и будет переприменяться при каждом входе." if eng == 1 else "")
        if not self.messagebox.askyesno(
                "Подтверждение",
                "Назначить \"%s\" по умолчанию для %d расширений/протоколов?\n"
                "Уже настроенных пропущено: %d.\nДвижок: %s%s"
                % (a.name, len(items), skipped,
                   "SetUserFTA" if eng == 0 else "политика DefaultAssociations", extra)):
            return
        self.cfg["include_protocols"] = self.var_protos.get()
        save_cfg(self.cfg)
        self._thread(self._apply_worker, items, eng)

    def _apply_worker(self, items, eng):
        self.log("=== Назначение: %d элементов, движок %s ===" %
                 (len(items), "SetUserFTA" if eng == 0 else "политика"))
        if eng == 1:
            ok, err = apply_policy(items, self.log)
        else:
            ok, err = apply_sufta(items, self.cfg, self.log,
                                  lambda i, n: self.q.put(("progress", (i, n))))
        self.log("=== Готово: успешно %d, ошибок/пропусков %d ===" % (ok, err))
        self.q.put(("status", "Готово: успешно %d, ошибок %d" % (ok, err)))
        self.q.put(("rows", None))

    def refresh_ucpd(self):
        def job():
            i = ucpd_info()
            pol = rget(ropen(HKLM, POLICY_KEY), POLICY_VAL) or ""

            if i["start"] == 4 and not i["running"]:
                ucpd_color = "#0f7b0f"
                ucpd_status_text = "ОТКЛЮЧЁН — готов к работе (SetUserFTA работает)"
            elif i["start"] == 3 and i["running"]:
                ucpd_color = "#a80000"
                ucpd_status_text = "АКТИВЕН — блокирует SetUserFTA (нужно отключить)"
            else:
                ucpd_color = "#b8860b"
                ucpd_status_text = "ПРОМЕЖУТОЧНОЕ СОСТОЯНИЕ — проверьте настройки"

            if "Disabled" in i["task"] or "Отключена" in i["task"]:
                task_status = "отключена (OK)"
            else:
                task_status = "активна (может вернуть UCPD)"

            ucpd_text = (
                "Статус UCPD: %s\n"
                "  - Файл драйвера: %s\n"
                "  - Режим запуска: Start: %s (%s)\n"
                "  - Драйвер запущен: %s\n"
                "  - Задача 'UCPD velocity': %s"
                % (ucpd_status_text,
                   "есть" if i["present"] else "отсутствует",
                   i["start"], START_NAMES.get(i["start"], "неизвестно"),
                   "ДА" if i["running"] else "нет",
                   task_status)
            )

            if pol:
                policy_color = "#5b5bd6"
                policy_text = ("Статус политики: ЗАДАНА (перекрывает ручной выбор при каждом входе)\n"
                               "  - Путь: %s" % pol)
            else:
                policy_color = "#0f7b0f"
                policy_text = "Статус политики: не задана (ручной выбор не перекрывается)"

            self.q.put(("ucpd", (ucpd_text, ucpd_color, policy_text, policy_color)))

        threading.Thread(target=job, daemon=True).start()

    def _ucpd(self, enable):
        title = "Восстановить UCPD" if enable else "Отключить UCPD"
        if not self.messagebox.askyesno(title, (
                "Вернуть исходный режим драйвера и включить задачу обслуживания?"
                if enable else
                "Отключить защитный драйвер Microsoft UCPD.sys и его задачу?\n\n"
                "Это снижает защиту ассоциаций файлов от подмены. Действие обратимо.\n"
                "Потребуется перезагрузка. Продолжить?")):
            return
        def job():
            ucpd_set(enable, self.cfg, self.log)
            i = ucpd_info()
            if i["start"] == 4 and not i["running"]:
                color = "#0f7b0f"
                status = "ОТКЛЮЧЁН — готов к работе"
            elif i["start"] == 3 and i["running"]:
                color = "#a80000"
                status = "АКТИВЕН — блокирует SetUserFTA"
            else:
                color = "#b8860b"
                status = "ПРОМЕЖУТОЧНОЕ СОСТОЯНИЕ"
            text = ("Статус UCPD: %s\n"
                    "  - Start: %s (%s)\n"
                    "  - Драйвер запущен: %s\n"
                    "  - Задача: %s"
                    % (status, i["start"], START_NAMES.get(i["start"], "?"),
                       "ДА" if i["running"] else "нет", i["task"]))
            self.q.put(("ucpd", (text, color,
                                 "Статус политики: (не обновлялся)", "#606060")))
            self.log("Перезагрузите компьютер, чтобы изменение вступило в силу.")
        self._thread(job)


def main():
    if os.name != "nt":
        print("Только Windows.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if len(sys.argv) > 1 and sys.argv[1].startswith("-"):
        args = build_parser().parse_args()
        cli_main(args)
        return

    MainWindow()


if __name__ == "__main__":
    main()