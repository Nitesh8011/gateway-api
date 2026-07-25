#!/usr/bin/env python3
"""
Mint a demo JWT signed with certs/jwt-signing-key.pem, matching the JWKS
published in 01-jwt-local-jwks.yaml's `gw-demo-jwt-jwks` ConfigMap.

Usage:
    pip install pyjwt cryptography
    python3 mint-jwt.py [--sub demo-user] [--expires-in 3600]

Prints a bearer token to stdout. Use it as:
    curl -H "Authorization: Bearer $(python3 mint-jwt.py)" ...
"""
import argparse
import time

import jwt  # PyJWT

PRIVATE_KEY_PATH = "../../certs/jwt-signing-key.pem"
KID = "gw-demo-jwt-key-1"
ISSUER = "https://gw-demo.local/issuer"
AUDIENCE = "gw-demo-app"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sub", default="demo-user")
    parser.add_argument("--expires-in", type=int, default=3600, help="seconds")
    parser.add_argument("--key", default=PRIVATE_KEY_PATH)
    args = parser.parse_args()

    with open(args.key) as f:
        private_key = f.read()

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": args.sub,
        "iat": now,
        "exp": now + args.expires_in,
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": KID},
    )
    print(token)


if __name__ == "__main__":
    main()
