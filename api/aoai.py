"""How the app signs in to the Azure AI model: an API key, or (better) the app's managed identity.

  AZURE_OPENAI_KEY=<key>            works everywhere (a secret to protect)
  AZURE_OPENAI_USE_IDENTITY=1       no key at all. The Function App's managed identity needs the role
                                    "Cognitive Services OpenAI User" on the AI resource.
If both are set, the key is used. Used by explain.py and assist.py.
"""
import os
import time

_tok = {"v": None, "exp": 0.0}


def configured():
    return bool(os.environ.get("AZURE_OPENAI_KEY") or os.environ.get("AZURE_OPENAI_USE_IDENTITY", "").lower() in ("1", "true", "yes"))


def auth_headers():
    """Headers that prove who we are to the AI endpoint, or None if neither a key nor identity is set up."""
    key = os.environ.get("AZURE_OPENAI_KEY", "")
    if key:
        return {"api-key": key}
    if os.environ.get("AZURE_OPENAI_USE_IDENTITY", "").lower() in ("1", "true", "yes"):
        now = time.time()
        if not _tok["v"] or now > _tok["exp"] - 300:
            from azure.identity import DefaultAzureCredential
            t = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default")
            _tok["v"], _tok["exp"] = t.token, float(t.expires_on)
        return {"Authorization": "Bearer " + _tok["v"]}
    return None
