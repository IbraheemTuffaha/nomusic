# nomusic — Privacy Policy

_Last updated: &lt;DATE&gt;_

> Ready-to-host privacy policy required for the Chrome Web Store listing. Host it
> at a stable public URL (e.g. `https://nomusic.<DOMAIN>/privacy`) and put that
> URL in the Web Store "Privacy practices" tab. Replace `<DOMAIN>`, `<DATE>`,
> `<N>` (= deployed `NOMUSIC_CACHE_TTL_DAYS`), and `<CONTACT_EMAIL>` before
> publishing.

nomusic is a Chrome extension that removes the background music from the video on
the page you are watching and plays a vocals-only version of its audio. This
policy explains what data the extension handles.

## What we collect
To process a video, the extension sends the following to the nomusic backend
server (`https://nomusic.<DOMAIN>`), operated by the extension's developer:
- The URL of the video on the page you choose to process.
- The processing options you select (separation model and which stems to keep).

We do NOT collect: your name, email, account credentials, IP-based identity
profiles, browsing history, page text, form data, cookies, or any other personal
information. The extension has no user accounts and no login.

The extension reads the current page's video element and player only on the
specific streaming sites it supports, solely to mute the original audio and
synchronize the vocals-only audio. This page content is processed locally in your
browser and is not transmitted, except for the video URL described above.

## How we use it
The video URL and options are used only to download and process that video on the
server and stream the resulting vocals-only audio back to your browser. We do not
use this data for advertising, profiling, analytics, or any purpose unrelated to
this single function. We do not sell or rent data to anyone.

## Server-side caching and retention
To avoid reprocessing the same video, the server temporarily caches the downloaded
source and the generated audio, keyed to the video URL and options. Cached files
are automatically deleted after &lt;N&gt; day(s) by an automatic cleanup process. We do
not associate cached files with any user identity.

## Sharing
We do not share your data with third parties. The video URL is sent only to our
own backend. The backend reaches the third-party video site (e.g. YouTube) to
download the video you requested; your interaction with those sites is governed by
their own privacy policies. Traffic to our backend is proxied through Cloudflare,
which may process connection metadata as our infrastructure provider.

## Security
All communication between the extension and the backend uses HTTPS. The backend is
not publicly browsable and exposes only the processing endpoints.

## Analytics / tracking
None. The extension contains no analytics, tracking, or advertising code.

## Your choices
Processing only happens when you click the in-page control. You can change or
self-host the backend URL in the extension settings, or uninstall the extension at
any time.

## Limited Use
nomusic's use of information received from Google APIs and from users adheres to
the Chrome Web Store User Data Policy, including the Limited Use requirements.

## Contact
Questions: &lt;CONTACT_EMAIL&gt;
