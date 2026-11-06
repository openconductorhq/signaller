"""Named presets: saved setups in presets.json, the active one's config living in console.json."""
import json
import os
import sys

from keys import write_json
from routing import config_from_json
from rulelang import fields_of, parse_cond

PRESET_FORMAT = 1
NAME_MAX = 60
TAGLINE_MAX = 200
DEFAULT_NAME = "my setup"
RESERVED = ("preview", "activate", "export")  # reserved to keep /ui/api/presets/<name> URLs unambiguous (route order already stops any real shadowing)
BAD_SUFFIX = ".bad"
BAD_BANNER = ('Your presets couldn\'t be read; they were moved to presets.json.bad '
              'and your current setup is kept as "my setup".')
SAVE_BANNER = 'Your presets file couldn\'t be written; presets won\'t survive a restart, but your current setup is kept.'


def fresh() -> dict:
    return {"active": DEFAULT_NAME, "presets": [{"name": DEFAULT_NAME, "tagline": "", "config": None}]}


def _valid(store) -> bool:
    ps = store.get("presets") if isinstance(store, dict) else None
    # An inactive preset always carries its config. The active one's, when set, is a snapshot left by an
    # interrupted switch (activate_preset stores it before console.json changes): it is ignored — the live
    # config is console.json's — and rewritten by the next switch.
    return (isinstance(ps, list) and isinstance(store.get("active"), str) and bool(ps)
            and all(isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("tagline"), str)
                    and (p.get("config") is None or isinstance(p.get("config"), dict)) for p in ps)
            and len({p["name"] for p in ps}) == len(ps)
            and all(p["config"] is not None or p["name"] == store["active"] for p in ps))


def load(config_dir: str) -> tuple[dict, str | None]:
    """(store, banner). A missing file starts a fresh "my setup" (written); an unreadable or invalid one is moved
    to presets.json.bad and the same fresh store comes back, with a banner — never a dead router."""
    path = os.path.join(config_dir, "presets.json")
    store, banner = None, None
    try:
        with open(path) as f:
            store = json.load(f)
        if not _valid(store):
            print("presets.json invalid", file=sys.stderr, flush=True)
            store, banner = None, BAD_BANNER
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:  # reason only, never the file's text
        print("presets.json unreadable: %s" % type(e).__name__, file=sys.stderr, flush=True)
        banner = BAD_BANNER
    if store is None:
        if banner:  # unreadable or invalid: out of the way, the fresh store replaces it
            try:
                os.replace(path, path + BAD_SUFFIX)
            except OSError:
                pass
        store = fresh()
        try:
            save(config_dir, store)
        except OSError as e:  # an unwritable dir must never cost the router its start
            print("presets.json unwritable: %s" % type(e).__name__, file=sys.stderr, flush=True)
            banner = banner or SAVE_BANNER
    return store, banner


def save(config_dir: str, store: dict) -> None:
    write_json(os.path.join(config_dir, "presets.json"), store, indent=2)


def portable(config_json: dict) -> dict:
    """A copy that can travel: no machine state (seeded, migrated_off) and no session-bound settings lines."""
    out = {k: v for k, v in config_json.items() if k not in ("seeded", "migrated_off")}
    keep = []
    for s in out.get("settings", []):
        w = s.get("when", "") if isinstance(s, dict) else None
        if isinstance(w, str):
            cond = parse_cond(w)
            if cond is not None and "session" in fields_of(cond):
                continue
        keep.append(s)  # anything unparseable is kept for config_from_json to reject with a reason
    out["settings"] = keep
    return out


def validated(config_json: dict):
    """What a stored, imported or exported preset's config must survive: portable(), then through
    config_from_json. ValueError with the reason when the importer would reject it."""
    return config_from_json(portable(config_json))


def _single_line(s: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in s)  # no newlines or control characters


def check_name(name: str) -> str:
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"a preset name must be 1–{NAME_MAX} characters")
    if not _single_line(name):
        raise ValueError("a preset name can't contain line breaks or control characters")
    if name.lower() in RESERVED:
        raise ValueError(f"'{name}' is a reserved preset name")
    return name


def check_tagline(tagline: str) -> str:
    if not isinstance(tagline, str) or len(tagline) > TAGLINE_MAX:
        raise ValueError(f"a preset tagline must be at most {TAGLINE_MAX} characters")
    if not _single_line(tagline):
        raise ValueError("a preset tagline can't contain line breaks or control characters")
    return tagline
