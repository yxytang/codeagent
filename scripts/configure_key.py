"""Send a hidden key to the local server; never write credentials to disk."""
import argparse
import getpass
import json
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()
key = getpass.getpass("DeepSeek API Key (hidden): ")
request = urllib.request.Request(
    f"http://127.0.0.1:{args.port}/api/settings",
    data=json.dumps({"api_key": key}).encode(),
    headers={"Content-Type": "application/json", "X-XCode-Client": "1"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    print("Key configured in server memory:", bool(result.get("has_api_key")))
except Exception:
    print("Unable to configure key. Check that the local server is running.")
    raise SystemExit(1)
finally:
    key = ""
