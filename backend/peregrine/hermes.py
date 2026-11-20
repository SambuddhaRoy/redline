"""Registers Peregrine as a provider in Nous Research's Hermes Agent, for WSL and native Windows installs.

The entry is additive (`providers.peregrine`): the user's own `model:` block is never touched. Use it with
`hermes chat --provider peregrine -m <model>` or `/model custom:peregrine:<model>` inside a session.
Runs from inside our WSL distro and reaches Windows and the other distros through WSL interop.
"""
import os
import re
import subprocess
import time

from . import GATEWAY_PORT

PROVIDER = "peregrine"
MARKER = "Peregrine (managed)"  # our entry's `name`; we only ever remove an entry that carries it
BASE_URL = f"http://127.0.0.1:{GATEWAY_PORT}/v1"  # all WSL distros share one network namespace; Windows gets localhost forwarding
SKIP_DISTROS = ("Peregrine", "docker-desktop")
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
HERMES_SH = 'h=$(command -v hermes || echo "$HOME/.local/bin/hermes"); exec "$h" "$@"'
# Hermes before 0.20 has no `config unset`; remove our entry with Hermes's own Python and PyYAML instead.
UNSET_PY = f"""
import os, sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open(p)) or {{}}
prov = c.get("providers") or {{}}
if str((prov.get("{PROVIDER}") or {{}}).get("name", "")).startswith("{MARKER}"):
    prov.pop("{PROVIDER}")
    open(p + ".tmp", "w").write(yaml.safe_dump(c, sort_keys=False, allow_unicode=True))
    os.replace(p + ".tmp", p)
"""


class HermesError(Exception):
    pass


def _run(argv, timeout=90, stdin=None) -> subprocess.CompletedProcess:
    # cwd on /mnt/c stops cmd.exe complaining about UNC paths; WSL_UTF8 stops wsl.exe printing UTF-16
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin, cwd="/mnt/c",
                          env={**os.environ, "WSL_UTF8": "1"})


def _sh(inst: dict, script: str, *args, stdin=None, timeout=90) -> subprocess.CompletedProcess:
    """Run a shell snippet where this install lives. Arguments go positionally, never into the script text."""
    if inst["kind"] == "wsl":
        return _run(["wsl.exe", "-d", inst["where"], "--exec", "/bin/sh", "-c", script, "sh", *args], timeout, stdin)
    return _run(["/bin/sh", "-c", script, "sh", *args], timeout, stdin)


def _hermes(inst: dict, *args) -> subprocess.CompletedProcess:
    r = _sh(inst, HERMES_SH, *args) if inst["kind"] == "wsl" else _run([inst["exe"], *args])
    if r.returncode != 0:
        raise HermesError(f"hermes {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def installs() -> list[dict]:
    found = []
    distros = _run(["wsl.exe", "-l", "-q"], timeout=30).stdout.replace("\x00", "").split()
    for d in distros:
        if d.startswith(SKIP_DISTROS):
            continue
        inst = {"kind": "wsl", "where": d}
        r = _sh(inst, HERMES_SH, "--version")
        if r.returncode == 0 and (m := VERSION.search(r.stdout)):
            found.append({**inst, "version": m.group(0).lstrip("v")})
    # /u: UTF-16 output, so a non-ASCII profile path survives (the OEM code page wouldn't decode as UTF-8)
    la = subprocess.run(["cmd.exe", "/u", "/c", "echo %LOCALAPPDATA%"], capture_output=True, timeout=30,
                        cwd="/mnt/c").stdout.decode("utf-16-le", errors="replace").strip()
    if la and "%" not in la:
        exe = _run(["wslpath", "-u", la + "\\hermes\\bin\\hermes.exe"]).stdout.strip()
        if os.path.exists(exe):
            r = _run([exe, "--version"])
            if r.returncode == 0 and (m := VERSION.search(r.stdout)):
                found.append({"kind": "windows", "where": "Windows", "exe": exe, "version": m.group(0).lstrip("v")})
    for inst in found:
        inst["config"] = _config_path(inst)
        inst["connected"] = bool(inst["config"]) and _sh(inst, 'grep -qF "$1" "$2"', MARKER, inst["config"]).returncode == 0
    return found


def _config_path(inst: dict) -> str | None:
    try:
        p = _hermes(inst, "config", "path").stdout.strip().splitlines()[-1].strip()
    except (HermesError, IndexError):
        return None
    if inst["kind"] == "windows":
        p = _run(["wslpath", "-u", p]).stdout.strip()
    return p


def _version(inst: dict) -> tuple:
    return tuple(int(x) for x in inst["version"].split("."))


def connect(models: list[tuple[str, int]], default: str) -> list[dict]:
    """models: (served id, context length) pairs, each >= Hermes's 64K floor."""
    results = []
    for inst in installs():
        try:
            if not inst["config"]:
                raise HermesError("couldn't find its config.yaml")
            if inst["connected"]:  # start clean so models deleted since the last connect disappear
                _remove(inst)
            else:  # first connect: keep a copy, like Hermes's own .bak naming
                _sh(inst, 'cp "$1" "$1.bak.peregrine-$2"', inst["config"], time.strftime("%Y%m%d_%H%M%S"))
            keys = [("name", MARKER), ("base_url", BASE_URL), ("default_model", default)]
            keys += [(f"models.{mid}.context_length", str(ctx)) for mid, ctx in models]
            for k, v in keys:
                _hermes(inst, "config", "set", f"providers.{PROVIDER}.{k}", v)
            ok = _reachable(inst)
            results.append({**inst, "connected": True, "reachable": ok,
                            "use": f"hermes chat --provider {PROVIDER} -m {default}"})
        except (HermesError, subprocess.TimeoutExpired) as e:
            results.append({**inst, "error": str(e)})
    return results


def disconnect() -> list[dict]:
    results = []
    for inst in installs():
        if not inst["connected"]:
            continue
        try:
            _remove(inst)
            results.append({**inst, "connected": False})
        except (HermesError, subprocess.TimeoutExpired) as e:
            results.append({**inst, "error": str(e)})
    return results


def _remove(inst: dict) -> None:
    if _version(inst) >= (0, 20):
        _hermes(inst, "config", "unset", f"providers.{PROVIDER}")
    elif inst["kind"] == "wsl":
        r = _sh(inst, 'exec "$HOME/.hermes/hermes-agent/venv/bin/python" - "$1"', inst["config"], stdin=UNSET_PY)
        if r.returncode != 0:
            raise HermesError(r.stderr.strip()[-400:])
    else:
        raise HermesError("this Hermes version has no `config unset`; remove providers.peregrine by hand")


def _reachable(inst: dict) -> bool:
    if inst["kind"] == "wsl":
        script = 'curl -sf -m 5 "$1/models" >/dev/null 2>&1 || wget -q -T 5 -O /dev/null "$1/models"'
        return _sh(inst, script, BASE_URL).returncode == 0
    return _run(["curl.exe", "-sf", "-m", "5", BASE_URL + "/models"], timeout=30).returncode == 0
