"""Self-healing check for corrupted .pyc bytecode cache before app startup.

Runs as fyp.service's ExecStartPre. Raspberry Pi SD-card storage can tear a
bytecode-cache write on an unclean shutdown, leaving a truncated .pyc that
Python later fails to unmarshal ("bad marshal data"). Scoped to the packages
proven to hit this (sympy, torchvision, ultralytics) since they're writable
by the service user and can safely self-heal by recompiling on next import.
Never blocks startup: any error here is logged and swallowed.
"""
import marshal
import os

VENV_SITE_PACKAGES = "/home/ruf/fyp_project/venv/lib/python3.13/site-packages"
PACKAGES = ["sympy", "torchvision", "ultralytics"]


def main():
    removed = []
    for pkg in PACKAGES:
        root = os.path.join(VENV_SITE_PACKAGES, pkg)
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(".pyc"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "rb") as f:
                        f.read(16)
                        marshal.loads(f.read())
                except Exception as e:
                    try:
                        os.remove(path)
                        removed.append((path, repr(e)))
                    except OSError:
                        pass

    if removed:
        print(f"[bytecode-cache-check] removed {len(removed)} corrupted .pyc file(s):")
        for path, err in removed:
            print(f"[bytecode-cache-check]   {path} -> {err}")
    else:
        print("[bytecode-cache-check] OK, no corrupted bytecode cache found")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[bytecode-cache-check] check itself failed, continuing startup anyway: {e!r}")
