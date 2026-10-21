"""Example radar plugin. Enable it with  "radar": {"plugins": ["plugins.example_rules"]}.

A plugin is any importable module with:
  SIGNALS = {code: {"icon": ..., "title": ..., "desc": ...}}       # display metadata
  def detect(m, bars, i, cfg) -> list[str]                          # codes that fire on bar i

`m` is the metrics dict the declarative rules see (python3 radar.py --list-metrics),
`bars` the closed klines (dicts with t, o, h, l, c, qv), `i` the index of the bar being
evaluated and `cfg` the whole `radar` config section (put your own parameters under
cfg["plugin_params"]). The thresholds below are illustrative placeholders only.
"""

SIGNALS = {
    "INSIDE_BREAK": {"icon": "📐", "title": "Inside-bar break", "desc": "Close above the previous bar's high after an inside bar"},
}


def detect(m, bars, i, cfg):
    p = cfg.get("plugin_params", {}).get("inside_break", {"min_vr": 3.0})
    prev, prev2 = bars[i - 1], bars[i - 2]
    inside = prev["h"] <= prev2["h"] and prev["l"] >= prev2["l"]
    if inside and bars[i]["c"] > prev2["h"] and m["vr"] >= p["min_vr"]:
        return ["INSIDE_BREAK"]
    return []
