# Making modules

A module is a `.zip` with a `module.json` at its root (or in one folder inside). The launcher's **Modules** tab
installs it into `~/.local/share/MHLauncher/modules/library/<id>/` and applies it to the game folder (the one that
contains `UnrealEngine3/` and `Data/`).

```
module.json
files/<path in the game folder>          whole files the module replaces or adds
textures/<anything>                      raw texture data patched into an icon package
calligraphy.json                         new prototype / blueprint directory entries
calligraphy/<path inside Calligraphy/>   files added to (or replacing files in) Data/Game/Calligraphy.sip
(module.json "assets")                   new model names: Calligraphy asset types + AssetPackageCache.bin
strings/<lang>.json                      new text lines
```

## module.json

```json
{
  "format": 1,
  "id": "my-new-hero",
  "name": "My New Hero",
  "version": "1.0.0",
  "author": "Your name",
  "description": "Shown on the Modules tab.",
  "game": "Marvel Heroes Omega 1.52.0.1700",
  "files": ["UnrealEngine3/MarvelGame/CookedPCConsole/UC__MarvelTeamUp_Example_SF.upk"],
  "textures": [{"package": "ICO__MarvelUIIcons_SF.upk", "texture": "Item_Art329", "file": "textures/Item_Art329.dxt5"}],
  "calligraphy": "calligraphy.json",
  "strings": {"eng": "strings/eng.json"}
}
```

* **id** names the module's folder; installing a module with the same id again updates it (keeps on/off and order).
  Change **version** with every release: the launcher rebuilds a module's files when its version changes.
* **files**: paths are relative to the game folder, with `/`. The file in `files/<path>` replaces the game's file
  (or adds it if the game doesn't have it).
* **textures**: for icon packages that other mods also change (the Mod Manager rebuilds `ICO__MarvelUIIcons_SF.upk`).
  Instead of replacing the whole package, the module patches single textures inside it. The data must be exactly the
  texture's own mip data (same format and size, e.g. 40×40 DXT5 = 1600 bytes). Compressed packages work too
  (the launcher unpacks them and compresses the changed parts again). If a texture isn't found at that size, for
  example because another mod changed that icon, it is skipped and noted in the log; the rest still apply.
* **calligraphy**: `calligraphy.json` lists the directory entries for new prototypes and blueprints, in the order they
  are added. Keep the ids and guids the same from one version to the next: saves refer to prototypes by guid, so a
  changed guid makes the server forget heroes and items that use it.

  ```json
  {"prototypes": [{"path": "Powers/Player/MyHero/BigPunch.prototype", "id": "…16 hex digits…",
                   "guid": "…", "blueprint": "…", "flags": 2}],
   "blueprints": [{"path": "Entity/Items/Armor/Blueprints/MyHero/ArmorMyHeroTop.blueprint", "id": "…",
                   "guid": "…", "flags": 1}]}
  ```

  The files themselves go in `calligraphy/` with their path inside `Calligraphy/`. A file that already exists in the
  game (for example a team-up prototype the module changes) is replaced. An entry whose id is already in the
  directory isn't added twice.
* **assets** (MH Launcher 1.8.0 and later): new model names, for a hero or costume with its own model package.
  The client finds a costume's model through its Unreal class name: the costume prototype points to an asset in
  `Entity/Types/UnrealClass.type`, the asset's name `X` is loaded from `UC__X_SF.upk`, and `AssetPackageCache.bin`
  says which packages hold the class `marvelgamecontent.X` (parent class packages first).

  ```json
  "assets": [{"type": "Entity/Types/UnrealClass.type", "id": "…16 hex digits…", "guid": "…16 hex digits…",
              "name": "MarvelHB_MyHero", "class": "marvelgamecontent.MarvelHB_MyHero",
              "packages": ["uc__marvelteamup_sf", "uc__marvelhb_myhero_sf"]}]
  ```

  The asset goes into the type file inside `Calligraphy.sip`; `class` and `packages` go into
  `UnrealEngine3/MarvelGame/CookedPCConsole/AssetPackageCache.bin` (the guid is the class's key there too). Ship the
  package itself in `files/`. Like prototypes, keep the id and guid the same between versions. Turning the module off
  takes all three out again.
* **requires** (format 2): ids of modules this one builds on, e.g. `["playable-jubilee"]` for a hero that uses
  Jubilee's powers. The module is only applied while every one of them is installed and on; otherwise the Modules
  tab shows "needs …" and leaves it out.
* **format**: use `2` when the module has `assets` or `requires` (older launchers then say "update MH Launcher"
  instead of installing something they can't apply); `1` otherwise.
* **strings**: `{"<16-hex-digit string id>": "text"}`. The line goes into the `<lang>.all_…string` file chosen by the
  id's top two bits (like the game does). Only new ids are added; existing lines are never changed by modules.

## How applying works

For every game file a module touches, the launcher keeps the file as it was before (`modules/bases/`) and the md5 of
what it wrote. Apply rebuilds the file from that base with every enabled module, lowest in the list first.
Turning modules off (or releasing them before the Mod Manager applies) writes the base back. If something else changed
a file after a module wrote it, that version becomes the new base. `modules.log` in the logs folder lists every step.

The server reads the same `Calligraphy.sip`; the launcher copies it from the client before the server starts.
