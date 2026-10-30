# Native platform schedules

These receipts record reviewed videos scheduled through a platform's own UI.
They are evidence and experiment linkage, not a second publishing queue.
No workflow consumes them or republishes them.

Pinterest's current RSS publisher handles images only. Video Pins in this folder
are scheduled in Pinterest and must not also be appended to pins.csv. Their
product URLs carry the same experiment_id in utm_content as the corresponding
Instagram experiment. They use separately cleared music with attribution.

A scheduled_pin_url is an account-only scheduling record, not proof of a public
Pin. After publication, verify the actual public Pin, playback, music and product
destination before adding a published_post_id or permalink. Public IDs and
metrics remain null until observed. Pinterest reporting is manual for these
native video schedules; Instagram's existing analytics integration is unchanged.

Finished MP4s and detailed local evidence stay outside Git. The receipt retains
their SHA-256 hashes, source product links, rights evidence and dates so a future
analytics observation can be attached without guessing.
