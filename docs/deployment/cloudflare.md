# Remote access with Cloudflare Tunnel and Cloudflare Access

CopperChat listens on plain HTTP (container port 8080). For use outside your LAN put it
behind HTTPS. **Any HTTPS reverse proxy works** (Caddy, Traefik, nginx — set
`PUBLIC_BASE_URL` and `FORWARDED_ALLOW_IPS` to the proxy's address); this page describes
Cloudflare Tunnel because it needs no open ports and no certificates. Web Push and
installing the app on iOS both require HTTPS.

## 1. Create the tunnel

1. Cloudflare Zero Trust dashboard → Networks → Tunnels → *Create a tunnel* → type
   *Cloudflared*, name it `copperchat`.
2. Copy the tunnel token into `.env`: `CLOUDFLARE_TUNNEL_TOKEN=...`.
3. Add a *public hostname*, e.g. `copperchat.example.com`, service type `HTTP`, URL
   `copperchat:8080` (the compose service name — not `localhost`).
4. In `.env` set
   ```dotenv
   PUBLIC_BASE_URL=https://copperchat.example.com
   VAPID_SUBJECT=mailto:you@example.com
   ```
5. Start: `docker compose --profile tunnel up -d`.

WebSockets work through the tunnel without extra settings. The compose file gives the
`cloudflared` container a fixed address (`172.30.231.10` in network `172.30.231.0/24`) and
trusts only that address for `X-Forwarded-For`, so the login rate limit sees the real client
address. If the range clashes with a network on your host, set `COPPERCHAT_SUBNET` and
`CLOUDFLARED_IP` in `.env`.

You can drop the published port (`ports:` in `docker-compose.yml`) when you only use the
tunnel.

## 2. Protect it with Cloudflare Access

CopperChat always has its own login, but a second factor in front of it is recommended:

1. Zero Trust → Access → Applications → *Add an application* → *Self-hosted*, domain
   `copperchat.example.com`.
2. Policy: *Allow*, include your e-mail address (one-time PIN or your identity provider).
3. Session duration: e.g. 1 month, so the installed app does not ask every day.

Cloudflare Access sits in front of everything, including the WebSocket and the manifest. If
the installed app on iOS shows the Access login page instead of CopperChat, open the URL once
in Safari, sign in to Access there, then reopen the Home Screen app.

## 3. Install the app and enable notifications

- Desktop Chrome/Edge: install icon in the address bar.
- Android: browser menu → *Install app* / *Add to Home screen*.
- iPhone/iPad (**iOS 16.4 or later**): Safari → Share → *Add to Home Screen*, then open
  CopperChat from the Home Screen. Push only works in the installed app on iOS.

Then Settings → Notifications → *Enable notifications*. You get a notification when a run
finishes, when the agent needs your attention (an error, or its last message is a
question), and when a rate-limit window reopens — only while no CopperChat window is
visible. Tapping a notification opens the project. Signing out turns notifications off on
that device.
