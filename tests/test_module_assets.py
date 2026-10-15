"""Modules that add model names: Calligraphy asset types and AssetPackageCache.bin.

    python3 -B -m unittest discover -s tests -v                 (from the repository folder)
    MHL_TEST_GAME=/path/to/Marvel\\ Heroes  ...                 also round-trips the game's real files

Everything here runs on a made-up game folder: the module is applied, turned off, and the files must come back
byte for byte.
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from mhlauncher import modules as M  # noqa: E402

GAME = os.environ.get("MHL_TEST_GAME")
TYPE = "Entity/Types/UnrealClass.type"


def make_sip(files):
    """A Calligraphy.sip holding `files` {name: raw bytes}, stored the way the launcher stores entries."""
    names = sorted(files)
    first = names[0]
    blob = M.lz4_store(files[first])
    nb = first.encode()
    head = struct.pack("<IIi", 0x53495020, 1, 1) + struct.pack("<Qi", M.data_id(first), len(nb)) + nb
    head += struct.pack("<iiii", 1000, 0, len(blob), len(files[first]))
    sip = M.Sip(head + blob)
    for n in names[1:]:
        sip.put(n, files[n])
    return sip.build()


def type_file(assets):
    out = bytearray(b"TYP\x0b" + struct.pack("<H", len(assets)))
    for aid, guid, name in assets:
        out += struct.pack("<QQB", aid, guid, 0) + struct.pack("<H", len(name)) + name.encode()
    return bytes(out)


def cache_file(guids, classes, rest=b"\0" * 8 + b"tail"):
    out = bytearray(struct.pack("<I", len(guids)))
    for g, s in guids:
        out += struct.pack("<Q", g) + M._cache_put(s)
    out += struct.pack("<I", len(classes))
    for s, pk in classes:
        out += M._cache_put(s) + struct.pack("<I", len(pk))
        for x in pk:
            out += M._cache_put(x)
    return bytes(out) + rest


def empty_directory(proto):
    return b"PDR\x00" + struct.pack("<I", 0) if proto else b"BDR\x00" + struct.pack("<I", 0)


ASSET = {"type": TYPE, "id": "7a0000000000beef", "guid": "6b0000000000cafe", "name": "MarvelHB_Test",
         "class": "marvelgamecontent.MarvelHB_Test", "packages": ["uc__marvelteamup_sf", "uc__marvelhb_test_sf"]}
PACKAGE = "UnrealEngine3/MarvelGame/CookedPCConsole/UC__MarvelHB_Test_SF.upk"


class Merge(unittest.TestCase):
    def test_add_assets_appends_and_skips_known(self):
        base = type_file([(1, 2, "MarvelPlayer_Storm")])
        out = M.add_assets(base, [ASSET, ASSET])
        self.assertEqual(out[:len(base)][6:], base[6:])
        self.assertEqual(M._type_ids(out), {1, 0x7a0000000000beef})
        self.assertEqual(M.add_assets(out, [ASSET]), out)

    def test_add_assets_keeps_signed_id_order(self):
        rows = [(0x8000000000000005, 9, "A"), (0x8100000000000000, 9, "B"), (0x0000000000000003, 9, "C"),
                (0x7f00000000000000, 9, "D")]                     # sorted as signed numbers, like the game's
        base = type_file(rows) + b"tail"
        self.assertEqual(M.add_assets(base, []), base)
        new = [dict(ASSET, id="%016x" % v, name="N%d" % k) for k, v in
               enumerate((0x8000000000000001, 0x0000000000000004, 0x7fffffffffffffff, 0x9000000000000000))]
        out = M.add_assets(base, new)
        self.assertTrue(out.endswith(b"tail"))
        n, = struct.unpack_from("<H", out, 4)
        p, ids = 6, []
        for _ in range(n):
            ids.append(struct.unpack_from("<q", out, p)[0])
            ln, = struct.unpack_from("<H", out, p + 17)
            p += 19 + ln
        self.assertEqual(n, 8)
        self.assertEqual(ids, sorted(ids))
        for aid, guid, name in rows:                               # existing rows keep their bytes
            raw = struct.pack("<QQB", aid, guid, 0) + struct.pack("<H", len(name)) + name.encode()
            self.assertIn(raw, out)

    def test_package_cache_round_trip_and_add(self):
        base = cache_file([(5, "marvelgamecontent.A")], [("marvelgamecontent.A", ["uc__a_sf"]), ("x.B", [])])
        self.assertEqual(M.add_package_cache(base, []), base)
        out = M.add_package_cache(base, [ASSET])
        guids, classes, rest = M.parse_package_cache(out)
        self.assertEqual(guids[-1], (struct.pack("<Q", 0x6b0000000000cafe), ASSET["class"]))
        self.assertEqual(classes[-1], (ASSET["class"], ASSET["packages"]))
        self.assertTrue(out.endswith(b"tail"))
        self.assertEqual(M.add_package_cache(out, [ASSET]), out)

    @unittest.skipUnless(GAME, "set MHL_TEST_GAME to the Marvel Heroes folder")
    def test_real_files_round_trip(self):
        with open(os.path.join(GAME, M.PACKAGE_CACHE), "rb") as f:
            data = f.read()
        self.assertEqual(M.add_package_cache(data, []), data)
        with open(os.path.join(GAME, M.CALLIGRAPHY), "rb") as f:
            sip = M.Sip(f.read())
        name = M._sip_name(sip, "Calligraphy/" + TYPE)
        raw = sip.read(name)
        self.assertEqual(M.add_assets(raw, []), raw)
        self.assertNotIn(int(ASSET["id"], 16), M._type_ids(raw))


class ApplyAndRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mhl-test-")
        self.saved = {k: getattr(M, k) for k in ("ROOT", "LIBRARY", "BASES", "REMOVED", "STATE")}
        M.ROOT = os.path.join(self.tmp, "modules")
        M.LIBRARY, M.BASES = os.path.join(M.ROOT, "library"), os.path.join(M.ROOT, "bases")
        M.REMOVED, M.STATE = os.path.join(M.ROOT, "removed"), os.path.join(M.ROOT, "state.json")
        self.game = os.path.join(self.tmp, "game")
        files = {"Calligraphy/Prototype.directory": empty_directory(True),
                 "Calligraphy/Blueprint.directory": empty_directory(False),
                 "Calligraphy/" + TYPE.replace("/", "\\"): type_file([(1, 2, "MarvelTeamUp_Jubilee")])}
        self.originals = {M.CALLIGRAPHY: make_sip(files),
                          M.PACKAGE_CACHE: cache_file([(2, "marvelgamecontent.MarvelTeamUp_Jubilee")],
                                                      [("marvelgamecontent.MarvelTeamUp_Jubilee",
                                                        ["uc__marvelteamup_sf", "uc__marvelteamup_jubilee_sf"])])}
        for rel, data in self.originals.items():
            os.makedirs(os.path.dirname(os.path.join(self.game, rel)), exist_ok=True)
            with open(os.path.join(self.game, rel), "wb") as f:
                f.write(data)
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "files", os.path.dirname(PACKAGE)))
        with open(os.path.join(src, "files", PACKAGE), "wb") as f:
            f.write(b"new package")
        with open(os.path.join(src, "module.json"), "w") as f:
            json.dump({"format": 1, "id": "hero-test", "name": "Test", "version": "1", "files": [PACKAGE],
                       "assets": [ASSET]}, f)
        self.src = src

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(M, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.game, rel), "rb") as f:
            return f.read()

    def test_on_then_off_restores_every_file(self):
        M.Library().install(self.src)
        lib = M.Library()
        mod = lib.find("hero-test")
        self.assertIn("Models & effects", mod.kinds())
        self.assertEqual(sorted(mod.targets()), sorted([M.CALLIGRAPHY, M.PACKAGE_CACHE, PACKAGE]))
        M.apply(self.game, lib=lib)
        sip = M.Sip(self.read(M.CALLIGRAPHY))
        self.assertIn(int(ASSET["id"], 16), M._type_ids(sip.read(M._sip_name(sip, "Calligraphy/" + TYPE))))
        _g, classes, _r = M.parse_package_cache(self.read(M.PACKAGE_CACHE))
        self.assertIn((ASSET["class"], ASSET["packages"]), classes)
        self.assertEqual(self.read(PACKAGE), b"new package")
        M.apply(self.game, lib=lib)                                  # again: nothing changes, nothing doubles
        _g, classes, _r = M.parse_package_cache(self.read(M.PACKAGE_CACHE))
        self.assertEqual(sum(c == ASSET["class"] for c, _ in classes), 1)

        lib.set_enabled(lib.find("hero-test"), False)
        M.apply(self.game, lib=lib)
        for rel, data in self.originals.items():
            self.assertEqual(self.read(rel), data, rel)
        self.assertFalse(os.path.exists(os.path.join(self.game, PACKAGE)))

    def test_release_all_restores_every_file(self):
        M.Library().install(self.src)
        lib = M.Library()
        M.apply(self.game, lib=lib)
        M.release_all(self.game, lib=lib)
        for rel, data in self.originals.items():
            self.assertEqual(self.read(rel), data, rel)
        self.assertFalse(os.path.exists(os.path.join(self.game, PACKAGE)))

    def test_requires_waits_for_the_other_module(self):
        with open(os.path.join(self.src, "module.json")) as f:
            meta = json.load(f)
        meta.update(format=2, requires=["playable-jubilee"])
        with open(os.path.join(self.src, "module.json"), "w") as f:
            json.dump(meta, f)
        M.Library().install(self.src)
        lib = M.Library()
        self.assertIn("playable-jubilee", lib.find("hero-test").waiting)
        self.assertEqual(M.apply(self.game, lib=lib), [])               # nothing goes in without Jubilee
        for rel, data in self.originals.items():
            self.assertEqual(self.read(rel), data, rel)

        dep = os.path.join(self.tmp, "dep")
        os.makedirs(dep)
        with open(os.path.join(dep, "module.json"), "w") as f:
            json.dump({"format": 1, "id": "playable-jubilee", "name": "Playable Jubilee", "version": "1"}, f)
        M.Library().install(dep)
        lib = M.Library()
        self.assertIsNone(lib.find("hero-test").waiting)
        M.apply(self.game, lib=lib)
        self.assertEqual(self.read(PACKAGE), b"new package")

        lib.set_enabled(lib.find("playable-jubilee"), False)             # Jubilee off: the hero goes out too
        self.assertTrue(lib.find("hero-test").waiting)
        M.apply(self.game, lib=lib)
        for rel, data in self.originals.items():
            self.assertEqual(self.read(rel), data, rel)
        self.assertFalse(os.path.exists(os.path.join(self.game, PACKAGE)))

    def test_newer_format_is_refused(self):
        with open(os.path.join(self.src, "module.json")) as f:
            meta = json.load(f)
        meta["format"] = M.FORMAT + 1
        with open(os.path.join(self.src, "module.json"), "w") as f:
            json.dump(meta, f)
        with self.assertRaises(ValueError):
            M.Library().install(self.src)

    def test_refuses_files_that_already_have_the_model(self):
        M.Library().install(self.src)
        lib = M.Library()
        with open(os.path.join(self.game, M.PACKAGE_CACHE), "wb") as f:
            f.write(M.add_package_cache(self.originals[M.PACKAGE_CACHE], [ASSET]))
        with self.assertRaises(ValueError):
            M.apply(self.game, lib=lib)


if __name__ == "__main__":
    unittest.main()
