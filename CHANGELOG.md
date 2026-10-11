# Changelog

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
