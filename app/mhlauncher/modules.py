"""Modules: bigger game changes (new heroes, new powers, ...) that the Mod Manager can't install.

A module is its own thing, separate from the Mods tab. It can change everything a new hero needs:

  * game files, replaced whole            (models, power effects: UC__*.upk, ICO__*.upk, ...)
  * icons, patched inside icon packages   (a few textures in ICO__MarvelUIIcons_SF.upk, which other mods share)
  * game data                             (Data/Game/Calligraphy.sip: new or changed prototypes and blueprints)
  * text                                  (new lines in Data/Game/Loco/<lang>.all/*.string)

Turning a module off (or removing it) puts every file back exactly as it was, byte for byte.

Shared files: the Mod Manager rebuilds some of the same files (icon packages, text) from its own originals and checks
they are still the way it left them. So modules always step back before the Mod Manager applies (release_all) and
come back afterwards (apply). Each game file a module touches remembers its "base" (the file as it was before any
module changed it) and the "output" modules wrote. A file that is still the output can be put back to its base; a file
that something else changed since is taken as the new base the next time.

The server reads the same Calligraphy.sip: the launcher copies it from the client before the server starts, so the
server always sees the modules too.

Module archive (.zip):
  module.json                      {"format": 1, "id", "name", "version", "author", "description",
                                    "files": ["UnrealEngine3/MarvelGame/CookedPCConsole/UC__X_SF.upk", ...],
                                    "textures": [{"package", "texture", "file"}],
                                    "calligraphy": "calligraphy.json", "strings": {"eng": "strings/eng.json"}}
  files/<path in the game folder>  whole-file replacements
  textures/...                     raw texture data (same size and format as the texture it replaces)
  calligraphy.json                 {"prototypes": [{"path", "id", "guid", "blueprint", "flags"}],
                                    "blueprints": [{"path", "id", "guid", "flags"}]}
  calligraphy/<path>               files added to / replacing files in Calligraphy.sip
  strings/<lang>.json              {"<string id, hex>": "text"}  (new lines only)
"""
import hashlib
import json
import os
import shutil
import struct
import tempfile
import time
import zipfile
import zlib

from . import lzo
from .config import DATA_DIR, LOG_DIR

ROOT = os.path.join(DATA_DIR, "modules")
LIBRARY = os.path.join(ROOT, "library")          # installed modules (one folder each)
BASES = os.path.join(ROOT, "bases")              # game files as they were before modules changed them
REMOVED = os.path.join(ROOT, "removed")          # removed modules (kept, just in case)
STATE = os.path.join(ROOT, "state.json")
FORMAT = 1
CALLIGRAPHY = "Data/Game/Calligraphy.sip"


def mlog(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "modules.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5b(data):
    return hashlib.md5(data).hexdigest()


def _write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mhl-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _save_json(path, data):
    _write_atomic(path, json.dumps(data, indent=2).encode("utf-8"))


# ======================================================================== LZ4 (pure Python, enough for .sip files)
def lz4_decompress(src, usize):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]
                i += 1
                ll += b
                if b != 255:
                    break
        dst += src[i:i + ll]
        i += ll
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    if len(dst) != usize:
        raise ValueError("bad LZ4 block")
    return bytes(dst)


def lz4_store(data):
    """A valid LZ4 block holding the data as-is (the game reads it like any other block)."""
    n = len(data)
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        r = n - 15
        while r >= 255:
            out.append(255)
            r -= 255
        out.append(r)
    out += data
    return bytes(out)


# ======================================================================== .sip archives (Calligraphy.sip)
def data_id(path):
    """The game's file id: Adler-32 | CRC-32 << 32, minus one, of the lower-cased path."""
    b = path.lower().encode()
    return ((zlib.adler32(b) | (zlib.crc32(b) << 32)) - 1) & 0xFFFFFFFFFFFFFFFF


class Sip:
    def __init__(self, data):
        self.data = data
        self.sig, self.ver, n = struct.unpack_from("<IIi", data, 0)
        p = 12
        self.entries = {}          # name -> [hash, mtime, offset, csize, usize, blob or None]
        for _ in range(n):
            h, = struct.unpack_from("<Q", data, p)
            p += 8
            ln, = struct.unpack_from("<i", data, p)
            p += 4
            name = data[p:p + ln].decode()
            p += ln
            mt, off, cs, us = struct.unpack_from("<iiii", data, p)
            p += 16
            self.entries[name] = [h, mt, off, cs, us, None]
        self.base = p

    def read(self, name):
        h, mt, off, cs, us, blob = self.entries[name]
        if blob is not None:
            return lz4_decompress(blob, us)
        return lz4_decompress(self.data[self.base + off:self.base + off + cs], us)

    def put(self, name, raw):
        modtime = max(e[1] for e in self.entries.values())
        blob = lz4_store(raw)
        self.entries[name] = [data_id(name), modtime, 0, len(blob), len(raw), blob]

    def build(self):
        order = sorted(self.entries.items(), key=lambda kv: kv[1][0])
        hdr = bytearray(struct.pack("<IIi", self.sig, self.ver, len(order)))
        body = bytearray()
        for name, (h, mt, off, cs, us, blob) in order:
            b = blob if blob is not None else self.data[self.base + off:self.base + off + cs]
            nb = name.encode()
            hdr += struct.pack("<Qi", h, len(nb)) + nb + struct.pack("<iiii", mt, len(body), len(b), us)
            body += b
        return bytes(hdr + body)


def _s16(s):
    b = s.replace("/", "\\").encode()
    return struct.pack("<H", len(b)) + b


def add_prototypes(directory, entries):
    """Prototype.directory: entries appended (id, guid, blueprint, flags, path)."""
    d = bytearray(directory)
    cnt, = struct.unpack_from("<I", d, 4)
    have = _directory_ids(directory, proto=True)
    added = 0
    for e in entries:
        pid = int(e["id"], 16)
        if pid in have:
            continue
        d += struct.pack("<QQQB", pid, int(e["guid"], 16), int(e["blueprint"], 16), int(e.get("flags", 2))) + _s16(e["path"])
        added += 1
    struct.pack_into("<I", d, 4, cnt + added)
    return bytes(d)


def add_blueprints(directory, entries):
    d = bytearray(directory)
    cnt, = struct.unpack_from("<I", d, 4)
    have = _directory_ids(directory, proto=False)
    added = 0
    for e in entries:
        bid = int(e["id"], 16)
        if bid in have:
            continue
        d += struct.pack("<QQB", bid, int(e["guid"], 16), int(e.get("flags", 1))) + _s16(e["path"])
        added += 1
    struct.pack_into("<I", d, 4, cnt + added)
    return bytes(d)


def _directory_ids(directory, proto):
    n, = struct.unpack_from("<I", directory, 4)
    p = 8
    ids = set()
    for _ in range(n):
        ids.add(struct.unpack_from("<Q", directory, p)[0])
        p += 8 + 8 + (8 if proto else 0) + 1
        ln, = struct.unpack_from("<H", directory, p)
        p += 2 + ln
    return ids


# ======================================================================== .string files (Data/Game/Loco)
STRING_FILES = {0: "3FFFFFFFFFFFFFFF", 1: "7FFFFFFFFFFFFFFF", 2: "BFFFFFFFFFFFFFFF", 3: "FFFFFFFFFFFFFFFF"}


def string_file_for(lang, sid):
    return f"Data/Game/Loco/{lang}.all/{lang}.all_{STRING_FILES[sid >> 62]}.string"


def add_strings(data, new):
    """Add new lines {id: text} to a .string file. Existing lines are kept byte for byte."""
    n, = struct.unpack_from("<H", data, 4)
    p = 6
    ents = []
    for _ in range(n):
        sid, nv, fp, off = struct.unpack_from("<QHHI", data, p)
        raw_vars = data[p + 16:p + 16 + 14 * max(nv - 1, 0)]
        ents.append((sid, nv, fp, off, raw_vars))
        p += 16 + 14 * max(nv - 1, 0)
    table_end = p
    page = bytearray(data[table_end:])
    have = {e[0] for e in ents}
    new = {sid: t for sid, t in new.items() if sid not in have}
    if not new:
        return data
    if n + len(new) > 0xFFFF:
        raise ValueError("too many lines for one text file")
    shift = 16 * len(new)
    out_ents = []
    for sid, nv, fp, off, raw_vars in ents:
        v = bytearray(raw_vars)
        for k in range(0, len(v), 14):
            fc, fp2, o2 = struct.unpack_from("<QHI", v, k)
            struct.pack_into("<QHI", v, k, fc, fp2, o2 + shift)
        out_ents.append((sid, nv, fp, off + shift, bytes(v)))
    new_end = table_end + shift
    for sid, txt in sorted(new.items()):
        out_ents.append((sid, 1, 0xFFFF, new_end + len(page), b""))
        page += txt.encode("utf-8") + b"\0"
    out_ents.sort(key=lambda x: x[0])
    if out_ents[0][3] != new_end:
        # the game expects the first line's text at the start of the page: lay the page out again in id order
        return _relayout_strings(data[:4], out_ents, data, table_end, shift, page, new_end)
    out = bytearray(data[:4]) + struct.pack("<H", len(out_ents))
    for sid, nv, fp, off, v in out_ents:
        out += struct.pack("<QHHI", sid, nv, fp, off) + v
    return bytes(out + page)


def _relayout_strings(head, ents, _data, _table_end, _shift, page, page_start):
    def cstr(off):
        i = off - page_start
        e = page.index(b"\0", i)
        return bytes(page[i:e + 1])
    table = bytearray(head) + struct.pack("<H", len(ents))
    body = bytearray()
    rows = []
    for sid, nv, fp, off, v in ents:
        main = cstr(off)
        vs = []
        for k in range(0, len(v), 14):
            fc, fp2, o2 = struct.unpack_from("<QHI", v, k)
            vs.append((fc, fp2, cstr(o2)))
        rows.append((sid, nv, fp, main, vs))
    table_len = 6 + sum(16 + 14 * len(r[4]) for r in rows)
    for sid, nv, fp, main, vs in rows:
        moff = table_len + len(body)
        body += main
        vb = bytearray()
        for fc, fp2, s in vs:
            vb += struct.pack("<QHI", fc, fp2, table_len + len(body))
            body += s
        table += struct.pack("<QHHI", sid, nv, fp, moff) + vb
    return bytes(table + body)


# ======================================================================== textures inside icon packages
def _upk_chunks(data):
    """(compression flags, position of the chunk table, [(uoff, usize, coff, csize)]) of a .upk."""
    if data[:4] != b"\xc1\x83\x2a\x9e":
        raise ValueError("not a game package")
    fl, = struct.unpack_from("<i", data, 12)
    p = 16 + fl + 48 + 16
    gens, = struct.unpack_from("<i", data, p)
    p += 4 + 12 * gens
    _engine, _cooker, comp, nch = struct.unpack_from("<iiIi", data, p)
    table = p + 16
    return comp, table, [struct.unpack_from("<iiii", data, table + 16 * i) for i in range(nch)]


def _upk_unpack(data):
    """The package as one uncompressed image (offsets inside it are the ones the package uses), and its chunks
    (None if it wasn't compressed)."""
    comp, _table, chunks = _upk_chunks(data)
    if not comp or not chunks:
        return data, None
    if comp != 2:
        raise ValueError(f"unsupported package compression {comp}")
    out = bytearray(data[:chunks[0][0]])
    for uoff, usz, coff, _csz in chunks:
        if uoff != len(out):
            raise ValueError("package chunks aren't in order")
        q = coff + 16
        _tag, _bs, _tc, total = struct.unpack_from("<Iiii", data, coff)
        blocks, got = [], 0
        while got < total:
            c, u = struct.unpack_from("<ii", data, q)
            q += 8
            blocks.append((c, u))
            got += u
        for c, u in blocks:
            out += lzo.decompress(data[q:q + c], u)
            q += c
        if len(out) != uoff + usz:
            raise ValueError("package chunk has the wrong size")
    return bytes(out), chunks


def _upk_repack(orig, chunks, unc, changed):
    """`orig` (compressed) with the chunks that hold the changed ranges [(start, end)] compressed again from `unc`."""
    _comp, table, _ = _upk_chunks(orig)
    head = bytearray(orig[:chunks[0][2]])
    body = bytearray()
    coff = chunks[0][2]
    for i, (uoff, usz, ocoff, ocsz) in enumerate(chunks):
        if any(a < uoff + usz and b > uoff for a, b in changed):
            tag, bs, _tc, _tu = struct.unpack_from("<Iiii", orig, ocoff)
            raw = unc[uoff:uoff + usz]
            blocks = [lzo.compress(raw[b:b + bs]) for b in range(0, usz, bs)]
            sizes = [len(raw[b:b + bs]) for b in range(0, usz, bs)]
            chunk = (struct.pack("<Iiii", tag, bs, sum(map(len, blocks)), usz)
                     + b"".join(struct.pack("<ii", len(c), u) for c, u in zip(blocks, sizes)) + b"".join(blocks))
        else:
            chunk = orig[ocoff:ocoff + ocsz]
        struct.pack_into("<iiii", head, table + 16 * i, uoff, usz, coff, len(chunk))
        body += chunk
        coff += len(chunk)
    return bytes(head + body)


def _upk_find_texture_data(data, texture, size):
    """Offset of the inline mip data of `texture` (exact size) in an uncompressed package image, or None."""
    if data[:4] != b"\xc1\x83\x2a\x9e":
        raise ValueError("not a game package")
    fl, = struct.unpack_from("<i", data, 12)
    h = 16 + fl
    _pkg_flags, ncount, noff, ecount, eoff = struct.unpack_from("<Iiiii", data, h)
    names = []
    p = noff
    for _ in range(ncount):
        ln, = struct.unpack_from("<i", data, p)
        p += 4
        if ln >= 0:
            s = data[p:p + ln - 1].decode("latin-1")
            p += ln
        else:
            s = data[p:p - 2 * ln - 2].decode("utf-16-le")
            p += -2 * ln
        p += 8
        names.append(s.lower())
    want = texture.lower()
    p = eoff
    for _ in range(ecount):
        cls, sup, outer, on, onn, arch, of1, of2, ssz, soff = struct.unpack_from("<iiiiiiIIii", data, p)
        ncnt, = struct.unpack_from("<i", data, p + 44)
        p += 48 + 4 * ncnt + 16 + 4
        if names[on] == want and onn == 0:
            blob_end = soff + ssz
            for q in range(soff, blob_end - 16):
                flg, cnt, sz, off = struct.unpack_from("<Iiii", data, q)
                if flg == 0 and cnt == size and sz == size and off == q + 16 and q + 16 + size <= blob_end:
                    return q + 16
    return None


# ======================================================================== modules
class Module:
    def __init__(self, folder):
        self.folder = folder
        self.id = os.path.basename(folder)
        self.name = self.id
        self.version = self.author = self.description = ""
        self.error = None
        self.m = {}
        try:
            self.m = _load_json(os.path.join(folder, "module.json"), None)
            if self.m is None:
                raise ValueError("no module.json")
            if int(self.m.get("format", 0)) > FORMAT:
                raise ValueError("made for a newer launcher – update MH Launcher")
            self.id = self.m.get("id") or self.id
            self.name = self.m.get("name") or self.id
            self.version = self.m.get("version", "")
            self.author = self.m.get("author", "")
            self.description = self.m.get("description", "")
        except (OSError, ValueError) as e:
            self.error = str(e)

    def path(self, rel):
        full = os.path.normpath(os.path.join(self.folder, rel))
        if not full.startswith(os.path.normpath(self.folder) + os.sep):
            raise ValueError(f"bad path in module: {rel}")
        return full

    @property
    def files(self):
        return list(self.m.get("files", []))

    @property
    def textures(self):
        return list(self.m.get("textures", []))

    def calligraphy(self):
        rel = self.m.get("calligraphy")
        if not rel:
            return None
        spec = _load_json(self.path(rel), {})
        folder = self.path(os.path.join(os.path.dirname(rel), "calligraphy"))
        loose = []
        if os.path.isdir(folder):
            for dp, _ds, fs in os.walk(folder):
                for f in fs:
                    full = os.path.join(dp, f)
                    loose.append((os.path.relpath(full, folder).replace(os.sep, "/"), full))
        return spec, sorted(loose)

    def strings(self):
        out = {}
        for lang, rel in (self.m.get("strings") or {}).items():
            out[lang] = {int(k, 16): v for k, v in _load_json(self.path(rel), {}).items()}
        return out

    def targets(self):
        """Game files (relative to the game folder) this module changes."""
        t = set(self.files)
        t.update("UnrealEngine3/MarvelGame/CookedPCConsole/" + x["package"] for x in self.textures)
        if self.m.get("calligraphy"):
            t.add(CALLIGRAPHY)
        for lang, lines in self.strings().items():
            t.update(string_file_for(lang, sid) for sid in lines)
        return sorted(t)

    def kinds(self):
        k = []
        if self.m.get("calligraphy"):
            k.append("Game data")
        if self.files:
            k.append("Models & effects")
        if self.textures:
            k.append("Icons")
        if self.m.get("strings"):
            k.append("Text")
        return k

    def size(self):
        if getattr(self, "_size", None) is not None:
            return self._size
        total = 0
        for dp, _ds, fs in os.walk(self.folder):
            total += sum(os.path.getsize(os.path.join(dp, f)) for f in fs)
        self._size = total
        return total


class Library:
    def __init__(self):
        os.makedirs(LIBRARY, exist_ok=True)
        self.state = _load_json(STATE, {})
        self.state.setdefault("order", [])
        self.state.setdefault("enabled", [])
        self.state.setdefault("files", {})       # game file -> {"base": md5, "out": md5}
        self.modules = []
        found = {}
        for d in sorted(os.listdir(LIBRARY)):
            full = os.path.join(LIBRARY, d)
            if os.path.isdir(full):
                found[d] = Module(full)
        order = [d for d in self.state["order"] if d in found] + [d for d in found if d not in self.state["order"]]
        self.state["order"] = order
        self.modules = [found[d] for d in order]

    def save(self):
        os.makedirs(ROOT, exist_ok=True)
        _save_json(STATE, self.state)

    def enabled(self, mod):
        return os.path.basename(mod.folder) in self.state["enabled"]

    def set_enabled(self, mod, on):
        key = os.path.basename(mod.folder)
        en = [x for x in self.state["enabled"] if x != key]
        if on:
            en.append(key)
        self.state["enabled"] = en
        self.save()

    def active(self):
        """Enabled modules, lowest priority first (the top of the list is applied last and wins)."""
        return [m for m in reversed(self.modules) if self.enabled(m) and not m.error]

    def find(self, key):
        return next((m for m in self.modules if os.path.basename(m.folder) == key or m.id == key), None)

    # ------------------------------------------------------------- install / remove
    def install(self, archive):
        """Install a module archive (.zip) or folder. Returns the Module (turned on, at the top)."""
        tmp = tempfile.mkdtemp(prefix="mhl-module-", dir=ROOT)
        try:
            if os.path.isdir(archive):
                shutil.copytree(archive, os.path.join(tmp, "m"))
            else:
                with zipfile.ZipFile(archive) as z:
                    for info in z.infolist():
                        target = os.path.normpath(os.path.join(tmp, "m", info.filename))
                        if not target.startswith(os.path.join(tmp, "m") + os.sep):
                            raise ValueError("unsafe path in archive: " + info.filename)
                    z.extractall(os.path.join(tmp, "m"))
            src = os.path.join(tmp, "m")
            if not os.path.isfile(os.path.join(src, "module.json")):
                subs = [d for d in os.listdir(src) if os.path.isfile(os.path.join(src, d, "module.json"))]
                if len(subs) != 1:
                    raise ValueError("this isn't a module (no module.json)")
                src = os.path.join(src, subs[0])
            mod = Module(src)
            if mod.error:
                raise ValueError(mod.error)
            key = "".join(c if c.isalnum() or c in "-_." else "-" for c in mod.id)
            dest = os.path.join(LIBRARY, key)
            was_on = key in self.state["enabled"]
            if os.path.isdir(dest):
                old = dest + ".old"
                shutil.rmtree(old, ignore_errors=True)
                os.replace(dest, old)
                shutil.move(src, dest)
                shutil.rmtree(old, ignore_errors=True)
            else:
                shutil.move(src, dest)
            if key not in self.state["order"]:
                self.state["order"].insert(0, key)
            if key not in self.state["enabled"] and not was_on:
                self.state["enabled"].append(key)
            self.save()
            mlog(f"installed {mod.name} {mod.version} as {key}")
            return Module(dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def remove_folder(self, mod):
        key = os.path.basename(mod.folder)
        os.makedirs(REMOVED, exist_ok=True)
        dest = os.path.join(REMOVED, key + time.strftime("-%Y%m%d-%H%M%S"))
        shutil.move(mod.folder, dest)
        self.state["order"] = [x for x in self.state["order"] if x != key]
        self.state["enabled"] = [x for x in self.state["enabled"] if x != key]
        self.save()
        mlog(f"removed {key} (kept in {dest})")
        return dest


# ======================================================================== apply / release
def _base_path(rel):
    return os.path.join(BASES, rel.replace("/", os.sep))


def _patch_textures(data, m, textures):
    """Write the module's icon textures into a package (compressed or not). A texture that can't be found as
    expected (e.g. another mod changed that icon) is skipped and logged: the item just keeps its old icon."""
    unc, chunks = _upk_unpack(data)
    buf = bytearray(unc)
    changed = []
    for t in textures:
        with open(m.path(t["file"]), "rb") as f:
            pix = f.read()
        at = _upk_find_texture_data(bytes(buf), t["texture"], len(pix))
        if at is None:
            mlog(f"warning: {m.id}: icon {t['texture']} in {t['package']} isn't a {len(pix)}-byte texture "
                 f"(another mod may have changed it); keeping that icon as it is")
            continue
        buf[at:at + len(pix)] = pix
        changed.append((at, at + len(pix)))
    if not changed:
        return data
    return _upk_repack(data, chunks, bytes(buf), changed) if chunks else bytes(buf)


def _build(rel, base, mods_for_file):
    """The file `rel` with every active module's changes applied on top of `base` (bytes)."""
    if rel == CALLIGRAPHY:
        sip = Sip(base)
        pdir = sip.read("Calligraphy/Prototype.directory")
        bdir = sip.read("Calligraphy/Blueprint.directory")
        for m in mods_for_file:
            spec, loose = m.calligraphy()
            for name, full in loose:
                with open(full, "rb") as f:
                    sip.put("Calligraphy/" + name, f.read())
            bdir = add_blueprints(bdir, spec.get("blueprints", []))
            pdir = add_prototypes(pdir, spec.get("prototypes", []))
        sip.put("Calligraphy/Prototype.directory", pdir)
        sip.put("Calligraphy/Blueprint.directory", bdir)
        return sip.build()
    out = base
    for m in mods_for_file:
        if rel in m.files:
            with open(m.path(os.path.join("files", rel)), "rb") as f:
                out = f.read()
        mine = [t for t in m.textures if rel.endswith("/" + t["package"])]
        if mine:
            out = _patch_textures(out, m, mine)
        for lang, lines in m.strings().items():
            mine = {sid: txt for sid, txt in lines.items() if string_file_for(lang, sid) == rel}
            if mine:
                out = add_strings(out, mine)
    return out


def _string_ids(data):
    n, = struct.unpack_from("<H", data, 4)
    p, ids = 6, set()
    for _ in range(n):
        sid, nv = struct.unpack_from("<QH", data, p)
        ids.add(sid)
        p += 16 + 14 * max(nv - 1, 0)
    return ids


def _already_in(rel, data, mods_for_file):
    """The module (if any) whose changes are already in `data` -- e.g. it was installed by hand before.
    Taking such a file as the base would make turning the module off put the module's version back."""
    if not data:
        return None
    for m in mods_for_file:
        if rel == CALLIGRAPHY:
            spec, _loose = m.calligraphy()
            sip = Sip(data)
            have = _directory_ids(sip.read("Calligraphy/Prototype.directory"), proto=True)
            if any(int(e["id"], 16) in have for e in spec.get("prototypes", [])):
                return m
            continue
        if rel in m.files and _md5(m.path(os.path.join("files", rel))) == _md5b(data):
            return m
        mine = [t for t in m.textures if rel.endswith("/" + t["package"])]
        if mine:
            unc, _chunks = _upk_unpack(data)
            for t in mine:
                with open(m.path(t["file"]), "rb") as f:
                    pix = f.read()
                at = _upk_find_texture_data(unc, t["texture"], len(pix))
                if at is not None and unc[at:at + len(pix)] == pix:
                    return m
        for lang, lines in m.strings().items():
            mine = [sid for sid in lines if string_file_for(lang, sid) == rel]
            if mine and set(mine) <= _string_ids(data):
                return m
    return None


def plan(game_root, lib=None):
    """{game file: [modules that change it]} for the active modules, plus files no active module wants any more."""
    lib = lib or Library()
    want = {}
    for m in lib.active():
        for rel in m.targets():
            want.setdefault(rel, []).append(m)
    gone = [rel for rel in lib.state["files"] if rel not in want]
    return lib, want, gone


def _current(game_root, rel):
    full = os.path.join(game_root, rel.replace("/", os.sep))
    return full, (_md5(full) if os.path.isfile(full) else None)


def release_all(game_root, progress=None, lib=None):
    """Put every file modules changed back to its base (before the Mod Manager applies, or when modules are off)."""
    lib = lib or Library()
    changed = []
    for rel, rec in list(lib.state["files"].items()):
        full, cur = _current(game_root, rel)
        if progress:
            progress(None, "Taking module changes out: " + os.path.basename(rel))
        if cur == rec.get("out"):
            bp = _base_path(rel)
            if rec.get("base") is None:          # the file didn't exist before the module added it
                os.remove(full)
            elif os.path.isfile(bp) and _md5(bp) == rec["base"]:
                with open(bp, "rb") as f:
                    _write_atomic(full, f.read())
            else:
                mlog(f"release: base copy of {rel} is missing; leaving the module's version")
                continue
            changed.append(rel)
            try:
                os.remove(_base_path(rel))
            except OSError:
                pass
        else:
            mlog(f"release: {rel} was changed by something else since; taking it as it is")
        del lib.state["files"][rel]
    lib.save()
    return changed


def apply(game_root, progress=None, lib=None):
    """Make the game files match the active modules. Returns a list of the files written or restored."""
    lib, want, gone = plan(game_root, lib)
    done = []
    for rel in gone:                              # files no active module needs: back to their base
        rec = lib.state["files"][rel]
        full, cur = _current(game_root, rel)
        if cur == rec.get("out"):
            bp = _base_path(rel)
            if rec.get("base") is None:
                os.remove(full)
            else:
                with open(bp, "rb") as f:
                    _write_atomic(full, f.read())
            done.append(rel)
        del lib.state["files"][rel]
        try:
            os.remove(_base_path(rel))
        except OSError:
            pass
    for rel, mods_for_file in want.items():       # check first, so nothing is half done
        full, cur = _current(game_root, rel)
        rec = lib.state["files"].get(rel)
        if cur and not (rec and cur == rec.get("out")):   # this file would become a new base
            with open(full, "rb") as f:
                hit = _already_in(rel, f.read(), mods_for_file)
            if hit:
                mlog(f"apply refused: {rel} already has {hit.id} in it")
                raise ValueError(
                    f"{os.path.basename(rel)} already has {hit.name} in it (installed by hand, or by an "
                    f"older version). Put the game's original {os.path.basename(rel)} back, then press "
                    f"Apply changes again. Otherwise turning {hit.name} off couldn't take it out.")
    for rel, mods_for_file in want.items():
        full, cur = _current(game_root, rel)
        rec = lib.state["files"].get(rel)
        bp = _base_path(rel)
        if rec and cur == rec.get("out") and rec.get("mods") == [m.id + "@" + m.version for m in mods_for_file]:
            continue                              # already up to date
        if progress:
            progress(None, "Applying modules: " + os.path.basename(rel))
        if rec and cur == rec.get("out"):         # ours, but the module set changed: rebuild from the saved base
            base = open(bp, "rb").read() if rec.get("base") else b""
        else:                                     # first time, or something else changed it: that's the new base
            if cur is None and rel != CALLIGRAPHY and not any(rel in m.files for m in mods_for_file):
                raise ValueError(f"{rel} is missing from the game folder")
            base = open(full, "rb").read() if cur else b""
            os.makedirs(os.path.dirname(bp), exist_ok=True)
            if cur:
                _write_atomic(bp, base)
            rec = {"base": cur}
        out = _build(rel, base, mods_for_file)
        _write_atomic(full, out)
        rec.update(out=_md5b(out), mods=[m.id + "@" + m.version for m in mods_for_file])
        lib.state["files"][rel] = rec
        lib.save()
        done.append(rel)
        mlog(f"apply: {rel} <- {', '.join(m.id for m in mods_for_file)}")
    lib.save()
    return done


def needs_apply(game_root, lib=None):
    lib, want, gone = plan(game_root, lib)
    if gone:
        return True
    for rel, mods_for_file in want.items():
        rec = lib.state["files"].get(rel)
        if not rec or rec.get("mods") != [m.id + "@" + m.version for m in mods_for_file]:
            return True
        if _current(game_root, rel)[1] != rec.get("out"):
            return True
    return False


ARCHIVE_EXTS = (".zip", ".mhmodule")


def downloads_archives(limit=40):
    out = []
    for d in (os.path.expanduser("~/Downloads"), os.path.expanduser("~/Desktop")):
        try:
            for f in os.listdir(d):
                full = os.path.join(d, f)
                if f.lower().endswith(ARCHIVE_EXTS) and os.path.isfile(full):
                    try:
                        with zipfile.ZipFile(full) as z:
                            if any(n.endswith("module.json") and n.count("/") <= 1 for n in z.namelist()):
                                out.append(full)
                    except (zipfile.BadZipFile, OSError):
                        pass
        except OSError:
            pass
    out.sort(key=os.path.getmtime, reverse=True)
    return out[:limit]
