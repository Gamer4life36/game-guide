"""Mission lists read from an installed game's own files.

Many new or early-access games have no wiki quest list. Unreal Engine 5 games ship an IoStore
index (.utoc) whose directory tree names every asset, and quest assets usually live under a
Quests/ or Missions/ folder. We list those, humanize the IDs, and mark the ones that appear in
the player's local save (%LOCALAPPDATA%/<Project>/Saved/SaveGames).
"""
import glob
import os
import re
import struct

MAGIC = b"-==--==--==--==-"
QUEST_DIR = re.compile(r"(?:^|/)(quests?|missions?|questlines?|storyline)(?:/|$)", re.I)
QUEST_FILE = re.compile(r"^(?:QID|Quest|QST|Mission|MSN|Q)_", re.I)
NOT_QUEST = re.compile(r"(?:^|_)(?:Board|Bundle|Book|Widget|WBP|Icon|Tex|Mat|Mesh|SFX|VFX|DT|Enum|Struct|Base|Parent|"
                       r"Template|Test|Debug|Curio|LoreBook)(?:_|$)", re.I)


def _fstring(b, o):
    n = struct.unpack_from("<i", b, o)[0]
    o += 4
    if n == 0:
        return "", o
    if n < 0:
        s = b[o:o - n * 2].decode("utf-16-le", "replace").rstrip("\0")
        return s, o - n * 2
    return b[o:o + n].decode("utf-8", "replace").rstrip("\0"), o + n


def utoc_files(path):
    """All file paths in an IoStore container's directory index (empty if encrypted or unreadable)."""
    b = open(path, "rb").read()
    if b[:16] != MAGIC:
        return []
    (hdr_size, entries, blocks, _block_sz, meth_count, meth_len, _cbs, dir_size, _parts) = struct.unpack_from("<9I", b, 20)
    flags = b[80]                             # 1 compressed, 2 encrypted, 4 signed, 8 indexed
    seeds, no_hash = struct.unpack_from("<I", b, 84)[0], struct.unpack_from("<I", b, 96)[0]
    if flags & 0x2 or not flags & 0x8 or not dir_size:
        return []
    o = hdr_size + entries * 12 + entries * 10 + seeds * 4 + no_hash * 4 + blocks * 12 + meth_count * meth_len
    if flags & 0x4:                           # signed
        hs = struct.unpack_from("<i", b, o)[0]
        o += 4 + hs * 2 + blocks * 20
    d = b[o:o + dir_size]
    p = 0
    mount, p = _fstring(d, p)
    n = struct.unpack_from("<i", d, p)[0]; p += 4
    dirs = [struct.unpack_from("<4I", d, p + i * 16) for i in range(n)]; p += n * 16
    n = struct.unpack_from("<i", d, p)[0]; p += 4
    files = [struct.unpack_from("<3I", d, p + i * 12) for i in range(n)]; p += n * 12
    n = struct.unpack_from("<i", d, p)[0]; p += 4
    names = []
    for _ in range(n):
        s, p = _fstring(d, p)
        names.append(s)
    NONE = 0xFFFFFFFF
    out, stack = [], [(0, mount.rstrip("/"))]
    while stack:
        idx, base = stack.pop()
        name, child, sib, fil = dirs[idx]
        here = base if name == NONE else f"{base}/{names[name]}"
        while fil != NONE:
            fn, fil, _ = files[fil]
            out.append(f"{here}/{names[fn]}")
        if sib != NONE:
            stack.append((sib, base))
        if child != NONE:
            stack.append((child, here))
    return out


def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def humanize(asset, group_words):
    s = re.sub(r"^(?:QID|Quest|QST|Mission|MSN|Q)_", "", asset, flags=re.I)
    parts = s.split("_")
    # Drop words already shown by the group heading (e.g. "Arcadia", "Adventure", "Bounty").
    drop = {w.lower() for w in group_words} | {"adventure", "adventures", "messagebased", "message", "based"}
    nums, words = [], []
    for part in parts:
        if not words and (part.isdigit() or re.fullmatch(r"\d+[A-Za-z]", part)):
            nums.append(part)
        elif part.lower() in drop and not words:
            continue
        else:
            words.append(part)
    text = " ".join(words)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = text[:1].upper() + text[1:] if text else asset
    return (".".join(nums) + " " if nums else "") + text


def install_dir(steam_libs, appid):
    for lib in steam_libs:
        f = os.path.join(lib, f"appmanifest_{appid}.acf")
        if os.path.exists(f):
            m = re.search(r'"installdir"\s+"([^"]+)"', open(f, encoding="utf-8", errors="replace").read())
            if m and os.path.isdir(os.path.join(lib, "common", m.group(1))):
                return os.path.join(lib, "common", m.group(1))
    return None


def save_strings(project):
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), project, "Saved", "SaveGames")
    blob = b""
    for f in glob.glob(os.path.join(root, "**", "*.sav"), recursive=True):
        if os.sep + "backup" in f.lower() or "_archive" in f or "cloudsaves" in f:
            continue
        try:
            blob += open(f, "rb").read()
        except OSError:
            pass
    return blob


def obtained_dates(blob):
    """{quest id: 'YYYY-MM-DD HH:MM'} from the save's item JSON ("firstObtained")."""
    out = {}
    text = blob.decode("latin-1")
    for m in re.finditer(r"\"itemDef\": \"[^\"]*/((?:QID|Quest|Mission)_[A-Za-z0-9_]+)\.", text):
        d = re.search(r'"firstObtained": "(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)', text[m.end():m.end() + 800])
        if d:
            stamp = f"{d[1]}-{d[2]}-{d[3]} {d[4]}:{d[5]}"
            out[m.group(1)] = min(out.get(m.group(1), stamp), stamp)
    return out


def quests_for(game_dir):
    """{'groups': [...]} built from the game's .utoc indexes, or {'groups': []}."""
    groups, project = {}, None
    for utoc in glob.glob(os.path.join(game_dir, "*", "Content", "Paks", "*.utoc")):
        try:
            paths = utoc_files(utoc)
        except Exception:
            continue
        if paths:
            project = project or os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(utoc))))
        for p in paths:
            if not p.endswith(".uasset"):
                continue
            folder, fn = p.rsplit("/", 1)
            asset = fn[:-7]
            m = QUEST_DIR.search(folder)
            if not m or not QUEST_FILE.match(asset) or NOT_QUEST.search(asset):
                continue
            sub = [w for w in folder[m.end():].split("/") if w]
            if any(re.search(r"test|debug|dev", w, re.I) for w in sub):
                continue
            groups.setdefault(tuple(sub[:2]) or ("General",), set()).add(asset)
    if not groups:
        return {"groups": []}
    blob = save_strings(project) if project else b""
    obtained = obtained_dates(blob)
    out = []
    for key in sorted(groups, key=lambda k: (0 if re.search(r"adventure|main|story", " ".join(k), re.I) else 1,
                                             [natural(x) for x in k])):
        words = [w for part in key for w in re.split(r"(?<=[a-z])(?=[A-Z])", part)]
        name = " · ".join(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", k) for k in key)
        name = re.sub(r"^Adventures · ", "", name)
        items = []
        for a in sorted(groups[key], key=natural):
            items.append({"label": humanize(a, words + list(key)), "id": a,
                          "seen": bool(blob) and (a + ".").encode() in blob, "started": obtained.get(a, "")})
        # Numbered story steps first, in order; then the rest alphabetically.
        items.sort(key=lambda i: (0 if re.match(r"\d", i["label"]) else 1, natural(i["label"])))
        out.append({"name": name, "items": items})
    return {"groups": out, "local": True, "project": project, "hasSave": bool(blob)}
