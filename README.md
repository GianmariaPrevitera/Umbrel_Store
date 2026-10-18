# Gianmaria App Store

A community app store for [umbrelOS](https://umbrel.com).

| App | Description | Version |
| --- | --- | --- |
| [URL Follower](gianmaria-url-follower) | Monitors a list of web addresses, follows their redirects and publishes the ones that are online as plain text. | 0.4.0 |
| [Hello World](gianmaria-hello-world) | The template app from the official community app store template. | 1.0.0 |

## Add the store to umbrelOS

1. Open the **App Store** in umbrelOS.
2. Open the menu in the top right corner and choose **Community App Stores**.
3. Paste the address of this repository and confirm:

   ```
   https://github.com/GianmariaPrevitera/Umbrel_Store
   ```

4. The apps appear under **Gianmaria App Store**.

The repository must be public and the apps are read from the `master` branch.

## URL Follower

URL Follower keeps an eye on a list of web addresses. Every few minutes it checks each one, follows its redirects, and saves the final destination. Anyone holding a personal API key can then fetch the list of addresses that are currently online as plain text.

It is useful, for example, for addresses that change domain from time to time: the list always contains the latest working one.

### How it works

- **Checks run in the background.** A scheduler inside the app checks every address 30 seconds after the app starts and then every 10 minutes, even when the web panel is closed.
- **Redirects are followed and saved.** Up to 5 redirects are followed. If an address has moved, the list keeps the final destination instead of the original address.
- **Online means a final response between 200 and 399.** Anything else, including errors and timeouts, is offline.
- **Page content is never downloaded.** The app only reads the response status and headers.

### Pages and endpoints

| Address | Access | Response |
| --- | --- | --- |
| `/` | umbrelOS login | Management panel for addresses and users |
| `/api/...` | umbrelOS login | JSON API used by the panel |
| `/api=<key>` | Public | Plain text, one online address per line |

`<key>` is a personal API key of exactly 9 letters and digits, for example `A1b2C3d4E`. If the key is wrong, or the user is marked as unavailable, the response is an empty web page with status `200 OK`: nothing reveals whether a key exists.

### Management panel

The panel opens on the app's main page and is protected by the umbrelOS login, so the app has no password of its own.

- **URLs**: add an address, check one or all of them right away, delete.
- **Users**: create a user to get a 9-character API key, mark it as available or unavailable, delete it.

Each user only needs a key to read the list at `/api=<key>`; users cannot access the panel.

### Security

URL Follower sends requests to addresses chosen by its users, so it is built to avoid being used to reach your own network.

- **Public addresses only.** Loopback, private, link-local, multicast and other non-public ranges are rejected, including addresses reached through a redirect. All addresses a name resolves to are checked, and the connection goes straight to the verified address so the DNS answer cannot change afterwards.
- **Ports 80 and 443 only**, `http` and `https` only, with a maximum URL length of 2048 characters and no control characters.
- **Rejected redirects are never saved.** If a redirect points to a forbidden destination, the last valid address stays in the list.
- **Rate limits** per client: 60 requests per minute for the public list, 30 for single checks and additions, 3 for "check all".
- **Protection against other websites.** Every request that changes data must carry a custom header, which other sites cannot send from your browser.
- **API keys stay out of the logs.** Access logs show `/api=*********` instead of the key.
- **Strict response headers**, including a Content Security Policy and `nosniff`.

#### Check your installation

The panel and the JSON API are protected by the umbrelOS login, and only `/api=*` is public. This is configured with `PROXY_AUTH_WHITELIST` in [`docker-compose.yml`](gianmaria-url-follower/docker-compose.yml). After installing, open these addresses in a private window where you are **not** logged in to umbrelOS:

- `http://umbrel.local:4001/` should redirect to the umbrelOS login.
- `http://umbrel.local:4001/api/urls` should do the same. If it returns data, the management API is exposed: uninstall the app.
- `http://umbrel.local:4001/api=ABCDEFGHI` should return an empty page without asking for a login.

### Configuration

Settings are environment variables of the `web` service in [`docker-compose.yml`](gianmaria-url-follower/docker-compose.yml).

| Variable | Default | Description |
| --- | --- | --- |
| `CHECK_INTERVAL_MINUTES` | `10` | Minutes between automatic checks. `0` turns them off. |
| `DATA_DIR` | `/data` | Where the database is stored. |
| `WWW_DIR` | `/www` | Where the panel files are served from. |
| `PORT` | `8080` | Port the app listens on inside its container. |

The app runs in the official `python:3.12-alpine` image and uses only the Python standard library, so there is no custom image to build or publish. Addresses and users are stored in a SQLite database in the app's `data` folder and are removed when the app is uninstalled.

To run a check manually from an SSH session on your Umbrel:

```
docker exec gianmaria-url-follower_web_1 python /app/app.py --check-urls
```

### Repository layout

```
gianmaria-url-follower/
├── app/app.py              backend: checks, API, public list
├── www/index.html          management panel
├── www/app.js              panel logic
├── data/.gitkeep           keeps the data folder in the repository
├── docker-compose.yml      services and settings
├── umbrel-app.yml          app listing shown in umbrelOS
└── icon.svg                app icon
```

## Create your own store

This repository started from the [Umbrel Community App Store template](https://github.com/getumbrel/umbrel-community-app-store). To adapt it:

1. Set the store `id` and `name` in `umbrel-app-store.yml`. The name appears in umbrelOS as `<name> App Store`.
2. Start every app `id` with the store `id` (for example `gianmaria-url-follower`) and name the app folder the same way.
3. Describe the app in its `umbrel-app.yml` and define its services in its `docker-compose.yml`.
