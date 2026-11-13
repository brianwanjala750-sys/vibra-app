# Local sign-in

1. Start VIBRA and open **Account**.
2. Create an account with an email address and a password of 8 to 128 characters. New accounts are signed in immediately.
3. VIBRA stores password hashes and saved libraries in `instance/vibra.sqlite3`. Passwords are not stored as plain text.
4. The backend creates `instance/session.key` on first startup to sign login cookies. Keep the `instance` directory private and include it in local backups. Alternatively, set `VIBRA_SECRET_KEY` before starting the backend.
5. To use VIBRA over HTTPS, set `VIBRA_COOKIE_SECURE=1`. VIBRA rate-limits account creation, sign-in, and catalog requests. It does not yet provide email verification or password recovery.

Accounts are local to this VIBRA server; they are not Firebase or email-verified accounts. If the SQLite database is deleted, accounts and saved libraries are lost. If only the session key is lost, users can sign in again.

The public media catalog uses Internet Archive items whose uploader-declared license is Public Domain, CC0, CC BY 4.0, or CC BY-SA 4.0. Internet Archive does not independently verify every uploader's rights claim; check the linked item and license before reusing or redistributing it.

## Public PWA and Android release

The root `render.yaml` prepares VIBRA for Render. It selects a paid web-service plan and a 1 GB persistent disk because accounts and libraries are stored in SQLite. Render's free filesystem is ephemeral and cannot preserve this data. Hosting charges are set by Render and are not included with this project.

To publish:

1. Create a GitHub repository and upload the project files. `.gitignore` excludes `instance/`, which contains the database and session-signing key.
2. In Render, create a **Blueprint** from that repository and review the paid service and persistent-disk costs before confirming.
3. After deployment, open the HTTPS URL Render provides in Android Chrome. Use VIBRA's **Install app** button when it appears, or choose **Install app** / **Add to Home screen** from Chrome's menu.
4. A separate APK still needs Android packaging and signing after the public URL exists. This computer currently has no Android build tools installed, so a compiled APK cannot be produced here yet.

The service rate limits sign-in, registration, and catalog requests in memory, suitable for the single Render instance in this Blueprint. Use shared rate-limit storage before scaling to multiple instances. VIBRA does not yet support email verification or password recovery; users must keep their passwords safe.

The Blueprint is preparation only: publishing requires your GitHub and Render accounts. Do not share account passwords or private keys in chat. The PWA shell may load offline, but streaming, catalog, sign-in, and downloads require an internet connection.
