"""Safe switch injection into the SPA bootstrap; no app/config imports."""

import json
from collections.abc import Mapping


def spa_switches_script(switches: Mapping[str, object]) -> str:
    """The inline script for the page. Every '<', '>' and '&' in the JSON is
    written as a \\u escape, so no value can close the script tag or open a
    comment; ensure_ascii covers the two JavaScript line separators."""
    payload = json.dumps(switches, separators=(",", ":"), ensure_ascii=True)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<script>window.__ORB_SWITCHES__={payload};</script>"


def spa_index_with_switches(html: str, switches: Mapping[str, object]) -> str:
    """index.html with the switches script placed before its first script
    (the app's module script), or before </head> when there is none."""
    script = spa_switches_script(switches)
    for marker in ("<script", "</head>"):
        at = html.find(marker)
        if at != -1:
            return html[:at] + script + html[at:]
    return script + html
