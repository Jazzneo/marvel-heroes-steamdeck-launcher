# Changelog

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
