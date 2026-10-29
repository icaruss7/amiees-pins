# Buffer TikTok connection trial

The existing Free Buffer account lists TikTok `@amiees.com`, Pinterest and
Instagram. Their presence in the interface is not proof that publication works.
This stage adds a read-only connection check. It creates no drafts, media uploads,
scheduled posts or public content, and changes no existing queue or schedule.

## Credential setup

Use the Buffer personal key named `Amiees Growth OS`. The key created for this
trial expires November 27, 2026. It has only `account:read`, `posts:read`,
`posts:write` and `insights:read`; account changes, channel changes, ideas,
snippets and engagement permissions are disabled. Buffer keys apply across
connected channels, so the probe additionally pins the TikTok channel ID and
handle. The probe uses only account lookup, not posting permissions.

Store the key as the encrypted repository Actions secret `BUFFER_API_KEY` in
`icaruss7/amiees-pins`. Never put it in a source file, CSV, log, screenshot,
workflow input or chat. Replace the secret when rotating an expired key. No
routine scheduled key-expiry monitor is introduced in this trial.

## Run the connection check

In GitHub Actions, open **Check Buffer TikTok connection**, choose **Run workflow**
on **main**, then inspect the `connection` job. Code pushes and pull requests run
offline tests only. The manual connection job makes two kinds of read-only
queries at `https://api.buffer.com`: organization IDs and connected-channel
metadata. It refuses redirects and never prints the token, account email or
unexpected channel details. No GraphQL mutation is permitted by the probe.

Success means Buffer reports the exact pinned TikTok channel as connected and
unlocked. It does not verify TikTok's current ability to accept our video or
prove a public upload. If disconnected, refresh TikTok through Buffer and rerun.
If locked, review the account's channel allocation before changing a plan.

For a local check, securely supply `BUFFER_API_KEY` through the process
environment, then run:

```text
python buffer_connection.py --channel-id 673d4900da2e3cb64c96536a --handle amiees.com
```

## Next content test

After the connection passes, use one approved original video and its reviewed
caption. The first content transfer should create a Buffer draft using
`saveToDraft: true`, with no live schedule. Confirm media processing, the target
TikTok account, AI disclosure, caption, thumbnail frame and brand disclosure
before separately approving any publication. The draft handoff is not implemented
by this connection-check stage. Video/music and channel capabilities still need
verification in the real composer; no custom-app approval is assumed.

Keep Instagram and Pinterest on their current GitHub publishing paths. Buffer's
existing channel links do not require migrating or duplicating those queues.

## Validation and references

Run `python -m unittest -v test_buffer_connection` for offline identity,
connection-state and secret-redaction regressions.

- [Buffer API authentication](https://developers.buffer.com/guides/authentication.html)
- [Channel queries](https://developers.buffer.com/guides/your-first-post.html)
- [API reference and draft semantics](https://developers.buffer.com/reference.html)

Actual connection-test results are established by the manual GitHub run, not by
this setup document or the offline fixtures.
