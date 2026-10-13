# Changelog

## v1.7.0
- **New Modules tab: install new heroes and other big changes, and take them out again.** A module can add what a mod can't: new playable heroes, powers and gear with their game data (`Calligraphy.sip`) and new text, plus models, power effects and icons.
  - **Install module…**, turn on/off, order, **Apply changes**, **Remove**. PLAY applies pending changes by itself.
  - Turning a module off or removing it puts every game file it changed back exactly as it was.
  - The server always gets the same game data: modules are applied before the launcher copies `Calligraphy.sip` to the server.
  - Works together with the Mods tab: modules step back before the Mod Manager applies and come back on top, so the Mod Manager always finds its files the way it left them.
  - On a friend's server modules are taken out for that session; your next PLAY at home puts them back.
  - First module: **Playable Jubilee**. How to make modules: `docs/modules.md`.
- The sidebar fits the extra tab.

## v1.6.1
- **Fix: Remove on the Mods tab failed with just "RuntimeError"** when the mod was already turned off but the change wasn't applied yet. Remove now applies pending changes first (the Mod Manager only lets a mod go once none of its files are in the game), then removes it.
- Errors without details now say where the log is instead of only an error name.
- Friend's mods offer: fixed a misplaced full stop.

## v1.6.0
- **Add to Steam now looks like the real game.** The shortcut is called **Marvel Heroes Omega** and gets the game's library artwork: tall cover, wide banner, big background, logo and icon.
  - If Marvel Heroes Omega is in your Steam library, the official artwork comes from Steam's own cache on your Deck. Otherwise the launcher tries Steam's image servers (the game left the store in 2017, so some sizes may be missing). Anything still missing gets MH Launcher artwork.
  - Pressing it again on an existing shortcut (also one from an older launcher version) just puts the artwork on it.
  - In Gaming Mode it uses SteamOS's own "add to Steam" and then adds the artwork; with Steam closed it writes the shortcut itself (your `shortcuts.vdf` is backed up first).
  - The official artwork is never included in the download; it's only copied on your own machine.

## v1.5.1
- **Fix: PLAY on your own server showed "name 'skip_mods' is not defined".** The check for a friend's mods was accidentally also added to solo play in v1.5.0. It now only runs when you join a friend.

## v1.5.0
- **Cash shop points (G) without commands.** Server tab → **Cash shop points (G)…**: pick any account on your server (yours or a friend's), see its balance, and press **+1,000 / +10,000 / +100,000 G** or **Other amount…** (a minus amount takes Gs away). You can also set how many Gs new accounts start with.
  - Works with the server on or off; the new balance shows up at that player's next login. It won't change the balance of someone who is in the game right now, because the server would overwrite it when they log out.
  - Accounts that have never logged in have no wallet yet. Log in once first.
- **Your friend sees your mods.** Mods change the game on each player's own Deck, so before, friends only saw their own mods. Now:
  - When a friend presses PLAY to join you, their launcher asks whether to **copy your mods** (only the ones you have turned on and they're missing or have older versions of). It copies them, turns them on in your order and applies them, then starts the game.
  - Or on the **Mods tab → Friend's mods** any time.
  - Their own mods stay, and older copies are kept in the Mod Manager's `removed` folder.
  - The host decides: **Settings → Share my mods with friends** (on by default). Sharing only works while you host.
  - Both launchers need v1.5.0 or newer, and the friend needs the Mod Manager set up (the launcher offers it).

## v1.4.0
- **Move my heroes (solo ⇄ friend).** Saves live on the server, so your solo heroes and the ones on a friend's server were separate. Now the Friends tab has **Move my heroes**:
  - **Bring to friend**: copies your solo heroes, items and stash to your account on your friend's server.
  - **Take home**: copies your heroes from your friend's server back into your solo save.
  - Shows both saves (heroes and items) before you choose, and asks once more before replacing anything.
  - A backup of the save that gets replaced is always made first (last 10 kept in `~/.local/share/MHLauncher/save-backups/`, on whichever Deck was changed).
  - Safe while the host keeps playing: it refuses while you're still in the game on that server (the server saves your heroes when you log out), and when the two servers are different MHServerEmu versions.
  - Needs your email and password on the friend's server; you can only read or replace your own save there. The transfer uses the encrypted login port, and 5 wrong passwords lock it for 5 minutes.
  - Both launchers need v1.4.0 or newer.

## v1.3.0
- **Deck to Deck: two Steam Decks play together with no router and no internet.** One Deck makes its own Wi-Fi hotspot right from the Friends tab (*How your friend reaches you → Deck to Deck*), and the other Deck presses **Find Deck**, picks it and types the 8-digit password. The address is filled in for you and *Play on my friend's server* switches on.
  - Your usual Wi-Fi comes back by itself when you stop hosting, leave, or close the launcher, and after a crash at the next start.
  - The hotspot is never started on its own after a reboot, and a wrong password isn't saved.
  - Works on LCD and OLED Decks, and on Linux PCs with NetworkManager. With firewalld (Bazzite), the game ports are also opened in the hotspot's own firewall zone and closed again automatically.
  - Steam Deck Wi-Fi can't do the old "ad-hoc" mode, so the launcher uses a hotspot, which works the same way and is more reliable.

## v1.2.4
- **The firewall now closes by itself when you're done playing.** No more forgetting it open:
  - It closes automatically when you turn off *Host for friends*, when the server stops, and when you quit the launcher – no password needed to close.
  - Even if the launcher crashes or is force-closed, it closes within a few seconds.
  - Opening uses temporary firewall rules (firewalld), so a reboot always clears them too.
  - When you start the server again while hosting, the launcher offers to open it again.
- New setting: **Close firewall when the server stops** (on by default).
- Firewall rules left open by v1.2.0–1.2.3 are offered for closing once at start.

## v1.2.3
- **Close the firewall after playing with friends.** When you turn off *Host for friends*, the launcher offers to close the firewall again (one password prompt). The Friends tab button switches to **Close firewall** while it's open, and quitting with the firewall still open asks *Close & quit* / *Keep open & quit*.
- Only the rules the launcher added are removed: opening now records exactly which ports (and the Tailscale trust) it added, so firewall rules you had before stay untouched.

## v1.2.2
- **Set up Tailscale** button on the Friends tab: turns on the Tailscale service (one password prompt), lets you control it without sudo, opens the login page, and shows your Tailscale address as soon as you're logged in.
- **Fix: symbols showed as empty boxes** (◀ ▶ ✓ → ⌫ …) on systems whose main font doesn't have them (e.g. Noto Sans on Bazzite). Missing symbols are now drawn with a bundled DejaVu Sans, and the selector arrows are drawn as shapes.

## v1.2.1
- **Fix: launcher was slow** after the Friends tab was added, especially while hosting. Firewall and Tailscale checks (which start `firewall-cmd` / `tailscale`) ran on every screen redraw; they now run in the background every few seconds, and the screen uses the cached result. Redraws went from up to ~1 s to a few milliseconds.

## v1.2.0
- **New Friends tab: play with a friend.**
  - **Host for friends** with an address to share, over the same network, **Tailscale**, **ZeroTier** or the **internet** (automatic router port forwarding with UPnP, with a CGNAT warning)
  - **Join** a friend: test the connection, create your account on their server, and PLAY logs you in to their server
  - The login bridge gives each player the right address (SiteConfig and the login ticket's game-server address), so the host and friends use the same server without any config editing
  - One password prompt opens the firewall (firewalld / ufw) for friends
  - Friends can only reach the login, server status and (optional) account creation; the dashboard and admin API stay local
- The game frontend now only listens on the network while hosting (local only otherwise)
- Better controller navigation in two-column screens

## v1.1.1
- **Mods setup fix:** the Windows .NET 8 runtime for MHO Extended Mod Manager is now set up completely (the base .NET Runtime and the Windows Desktop Runtime), with Microsoft's installer as a fallback. Every setup step is logged in `logs/modmanager.log`.

## v1.1.0
- **New Mods tab**, powered by [MHO Extended Mod Manager](https://github.com/leeper48/MHO-UPK-Tools) by leeper48:
  - one-time **Set up Mod Manager** (downloads the official release, SHA-256 checked)
  - **Install mod…** with your newest downloads (.zip / .7z / .rar) listed first
  - turn mods on/off with **A**, change their priority, see which mod wins a conflict
  - **Apply changes**, or just press **PLAY**: pending mod changes are applied before the game starts
  - **Remove** takes a mod out of the game first and keeps its files in a `removed` folder
  - **Full Mod Manager** opens the original window (editor, Nexus features) on the same library

## v1.0.0
- First release: server on/off, sign in and create accounts, automatic game login, client finder with version check, Proton / GE-Proton / umu / Wine, built-in login bridge (no Apache/nginx), controller-first UI with on-screen keyboard, Add to Steam.
- Detects leftover servers, busy ports (moves off 8080 when Steam holds it) and blocking web servers, with a one-prompt **Fix it for me**.
