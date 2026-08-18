from __future__ import annotations

import hashlib
import getpass
import secrets
import sys


def encode(password: str, iterations: int = 260_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, salt.hex(), digest.hex())


if __name__ == "__main__":
    password = sys.argv[1] if len(sys.argv) == 2 else getpass.getpass("管理员密码: ")
    if not password:
        raise SystemExit("密码不能为空")
    print(encode(password))
