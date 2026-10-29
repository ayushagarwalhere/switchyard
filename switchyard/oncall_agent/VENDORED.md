# WispOps (vendored)

This folder is [WispOps](https://github.com/ayushagarwalhere/wisprops) v1.3.0 by Ayush Agarwal (MIT, see LICENSE),
shipped inside Switchyard so `switchyard oncall` works with only Node.js 18.17+ installed.

`switchyard oncall` writes a WispOps config for every app you deployed with `switchyard deploy` (health URL,
restart, roll back to an earlier image, release and logs all go through `switchyard deploy ...`) and starts it.
Approving fixes by voice with Wispr Flow is optional: Switchyard turns on typed approvals unless you pass `--voice`.
