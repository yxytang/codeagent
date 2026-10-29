"""Send a hidden key to the local server; never write credentials to disk."""
import argparse
import getpass
import json
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--model-profile-id", help="Saved model connection ID; defaults to the active connection")
args = parser.parse_args()
key = getpass.getpass("Model API Key (hidden): ")
endpoint = "/api/models" if args.model_profile_id else "/api/settings"
payload = {"id": args.model_profile_id, "api_key": key} if args.model_profile_id else {"api_key": key}
request = urllib.request.Request(
    f"http://127.0.0.1:{args.port}{endpoint}",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "X-XCode-Client": "1"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    configured = next((profile.get("has_api_key") for profile in result.get("model_profiles", []) if profile["id"] == args.model_profile_id), False) if args.model_profile_id else result.get("has_api_key")
    print("Key configured in server memory:", bool(configured))
except Exception:
    print("Unable to configure key. Check that the local server is running.")
    raise SystemExit(1)
finally:
    key = ""
